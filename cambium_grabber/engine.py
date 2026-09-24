"""Core crawl/download engine: log in, walk every product category, pull
every file in every release, skip what's already on disk. Modeled on the
MikroTik grabber's engine (same retry/backoff, same atomic manifest, same
skip-if-already-downloaded logic) but discovery is a real link crawl instead
of brute-force version guessing, and every request needs an authenticated
session.

Concurrency defaults are deliberately modest (see DEFAULT_* below) - this
is hitting a vendor portal as a logged-in person, not an anonymous public
CDN. Cambium started returning real 429 Too Many Requests responses
(2026-09-24, mid-crawl) after not rate-limiting at all during earlier
testing - RateLimitBackoff below handles that: every download/discovery
thread shares one backoff clock, so a 429 anywhere pauses everything
together instead of each thread independently retrying and immediately
re-triggering the same limit.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import auth, discovery
from .state import Manifest

# A custom, self-identifying UA ("CambiumGrabber/1.0 ...") got the login POST
# silently rejected (200 OK, but the session never actually authenticates);
# a standard browser UA doesn't. User explicitly confirmed proceeding with
# this despite it being an active technical control, not just a ToS clause -
# see project notes/conversation history.
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
# Lowered from 4/4 after Cambium started actively rate-limiting (429s) -
# fewer concurrent streams means fewer requests/sec in the first place,
# on top of the shared backoff that reacts once a limit is actually hit.
DEFAULT_CATEGORY_WORKERS = 2
DEFAULT_DOWNLOAD_WORKERS = 2

_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')
_SIZE_RE = re.compile(r'([\d.]+)\s*([kKmMgGtT]?[bB])')
_SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}


def _sanitize(name: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", name).strip()


def parse_size(size_text) -> int:
    """"210.3 MB" / "127.7 kB" / "1.2 GB" -> bytes. Returns 0 for anything
    that doesn't match (better to undercount a size estimate than crash on
    a format Cambium's page happens to use that wasn't seen during testing).
    """
    if not size_text:
        return 0
    m = _SIZE_RE.search(size_text)
    if not m:
        return 0
    value, unit = m.groups()
    try:
        return int(float(value) * _SIZE_UNITS.get(unit.lower(), 1))
    except ValueError:
        return 0


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


class RateLimiter:
    """Thread-safe token bucket, shared across every download thread, so
    --max-mbps caps the crawl's *aggregate* throughput - not just one
    file's speed, which would do nothing useful with multiple concurrent
    workers each free to max out the connection on their own. Concurrency
    settings (--dl-workers/--category-workers) control how many streams run
    at once; this is the only thing that actually caps total bandwidth.
    """

    def __init__(self, bytes_per_sec):
        self.bytes_per_sec = bytes_per_sec
        self._lock = threading.Lock()
        self._tokens = bytes_per_sec
        self._last = time.monotonic()

    def consume(self, n: int):
        if not self.bytes_per_sec:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.bytes_per_sec, self._tokens + (now - self._last) * self.bytes_per_sec)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
            time.sleep(min(deficit / self.bytes_per_sec, 0.5))


class RateLimitBackoff:
    """Coordinates every thread's response to a 429 - shared, not
    per-thread. Without this, each thread's independent retry-with-backoff
    (a few seconds) just re-triggers the same site-wide limit the instant
    it wakes up, because every *other* thread is still hammering away at
    full speed in the meantime. One thread hitting a 429 means the whole
    crawl backs off together.

    Delay grows with consecutive hits (capped) and resets once the crawl's
    gone long enough without tripping the limit again - a Retry-After
    header, when Cambium sends one, is honored as a floor rather than
    guessed at.
    """

    def __init__(self, base_delay=30.0, max_delay=300.0, reset_after=90.0):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.reset_after = reset_after
        self._lock = threading.Lock()
        self._pause_until = 0.0
        self._consecutive_hits = 0
        self._last_hit = 0.0

    def wait_if_paused(self):
        while True:
            with self._lock:
                remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def trigger(self, retry_after=None) -> float:
        with self._lock:
            now = time.monotonic()
            if now - self._last_hit > self.reset_after:
                self._consecutive_hits = 0
            self._consecutive_hits += 1
            self._last_hit = now

            computed = min(self.base_delay * (2 ** (self._consecutive_hits - 1)), self.max_delay)
            try:
                server_delay = float(retry_after) if retry_after is not None else 0.0
            except (TypeError, ValueError):
                server_delay = 0.0
            delay = max(computed, server_delay)

            self._pause_until = max(self._pause_until, now + delay)
            return delay


class Engine:
    def __init__(self, output_dir: str, email: str, password: str,
                 category_workers: int = DEFAULT_CATEGORY_WORKERS,
                 download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
                 max_retries: int = 3, include_archive: bool = True,
                 category_filter=None, priority=None, max_mbps=None,
                 on_event=None, stop_flag=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.password = password
        self.category_workers = max(1, category_workers)
        self.download_workers = max(1, download_workers)
        self.max_retries = max_retries
        self.include_archive = include_archive
        self.category_filter = category_filter  # optional set of slugs to restrict to
        # Megabits/sec -> bytes/sec (standard bandwidth-cap unit, matches how
        # a datacenter connection's limit is usually quoted). None = no cap.
        self._rate_limiter = RateLimiter(max_mbps * 1_000_000 / 8) if max_mbps else None
        self._backoff = RateLimitBackoff()
        # Priority group/category names (case-insensitive, matched against
        # group, category name, or slug) - these get fully crawled (current
        # + archive) before anything else starts, rather than just sorted
        # first in a shared thread pool (which finishes in whatever order,
        # not submission order).
        self.priority = [p.strip().lower() for p in (priority or []) if p.strip()]
        self._on_event = on_event or (lambda evt: None)
        self._stop_flag = stop_flag or (lambda: False)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        pool_size = max(10, self.category_workers, self.download_workers)
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.session.mount("https://", adapter)

        self.session_cache = self.output_dir / ".session.json"
        self.manifest = Manifest(self.output_dir)
        self._progress_lock = threading.Lock()
        self._failed_files = []

    def _emit(self, kind: str, **payload):
        self._on_event({"type": kind, **payload})

    def log(self, message: str, level: str = "INFO"):
        self._emit("log", message=message, level=level)

    def _stopped(self) -> bool:
        try:
            return bool(self._stop_flag())
        except Exception:
            return False

    def _login(self):
        try:
            reauthenticated = auth.ensure_authenticated(self.session, self.email, self.password, self.session_cache)
        except auth.LoginError as e:
            self.log(f"Login failed: {e}", "ERROR")
            raise
        if reauthenticated:
            self.log("Logged in (fresh session).", "INFO")
        else:
            self.log("Reusing cached session.", "INFO")

    def _discover_with_reauth(self, fn, *args, max_rate_limit_attempts=12):
        """Call a discovery.py function, handling the two recoverable
        failure modes discovery.py can raise:

        - SessionExpired: the session died mid-crawl (a real risk crawling
          ~90 categories, which can outlast a session's lifetime) - log
          back in once and retry. Without this, an expired session silently
          parsed the login page as if it were the requested page (0
          releases found, no error) - looked exactly like the tool being
          stuck.
        - RateLimited: a 429 - back off (shared across every thread, see
          RateLimitBackoff) and retry, up to max_rate_limit_attempts times.
          Discovery requests are cheap and infrequent compared to file
          downloads, so a generous retry budget here costs little.
        """
        self._backoff.wait_if_paused()
        try:
            return fn(self.session, *args)
        except discovery.SessionExpired:
            self.log("Session expired mid-crawl, re-authenticating...", "WARNING")
            auth.login(self.session, self.email, self.password)
            auth.save_session(self.session, self.session_cache)
            return fn(self.session, *args)
        except discovery.RateLimited as e:
            for attempt in range(1, max_rate_limit_attempts + 1):
                delay = self._backoff.trigger(e.retry_after)
                self.log(f"Rate limited (429) during discovery, attempt {attempt}/{max_rate_limit_attempts} - "
                         f"backing off {delay:.0f}s...", "WARNING")
                self._backoff.wait_if_paused()
                try:
                    return fn(self.session, *args)
                except discovery.RateLimited as e2:
                    e = e2
                    continue
            raise

    # -- discovery ----------------------------------------------------
    def _categories(self):
        categories = discovery.discover_categories(self.session)
        if self.category_filter:
            categories = [c for c in categories if c.slug in self.category_filter]
        return categories

    def _is_priority(self, category: discovery.Category) -> bool:
        haystack = (category.group + " " + category.name + " " + category.slug).lower()
        return any(p in haystack for p in self.priority)

    def _split_priority(self, categories):
        priority_cats = [c for c in categories if self._is_priority(c)]
        other_cats = [c for c in categories if not self._is_priority(c)]
        return priority_cats, other_cats

    # -- dry run / sizing ------------------------------------------------
    def estimate(self):
        """Discovery-only pass: crawl every category and release, tally
        file counts/sizes, but don't download anything. Answers "how big
        is this and how much is left" before committing to a real run -
        useful given a full crawl across every product line can plausibly
        run into the hundreds of GB (see the MikroTik tool's archive for a
        sense of scale on a similarly old, many-version product catalog).
        """
        self._login()
        categories = self._categories()
        self.log(f"Found {len(categories)} product categories. Discovering releases (no downloads)...", "INFO")

        totals = {"files": 0, "bytes": 0, "new_files": 0, "new_bytes": 0}
        by_group = {}

        def process(category: discovery.Category):
            releases = self._discover_with_reauth(discovery.discover_current_releases, category)
            if self.include_archive:
                releases = releases + self._discover_with_reauth(discovery.discover_archive_releases, category)
            return category, releases

        with ThreadPoolExecutor(max_workers=self.category_workers) as ex:
            futures = [ex.submit(process, c) for c in categories]
            for fut in as_completed(futures):
                if self._stopped():
                    continue
                try:
                    category, releases = fut.result()
                except Exception as e:
                    self.log(f"Failed to size category: {e}", "ERROR")
                    continue

                group_stats = by_group.setdefault(category.group, {"files": 0, "bytes": 0, "new_files": 0, "new_bytes": 0})
                for release in releases:
                    group_id = f"{category.slug}/{release.release_id}"
                    for f in release.files:
                        size = parse_size(f.size_text)
                        already_have = self.manifest.has_file(group_id, _sanitize(f.filename))
                        totals["files"] += 1
                        totals["bytes"] += size
                        group_stats["files"] += 1
                        group_stats["bytes"] += size
                        if not already_have:
                            totals["new_files"] += 1
                            totals["new_bytes"] += size
                            group_stats["new_files"] += 1
                            group_stats["new_bytes"] += size

        totals["categories"] = len(categories)
        totals["by_group"] = by_group
        return totals

    # -- main entry points ---------------------------------------------
    def full_scan(self):
        """Crawl every category and download everything not already on
        disk, newest material first: every category's *current* releases
        are downloaded across the whole catalog before any category's
        *archive* (older) releases are even fetched. That way an
        interrupted or still-in-progress run has already grabbed the stuff
        that matters most - current firmware/docs/MIBs for every product -
        before it starts spending time backfilling decades-old PMP/PTP
        history.

        If priority groups/categories were given, they're fully finished
        (current + archive) before any non-priority category is even
        started, rather than just processed "first" in a shared thread pool
        (which finishes in whatever order, not submission order).

        There's no cheap "just tell me what's new" signal the way
        MikroTik's RSS/pointer files provide - the catalog itself is small
        enough (under 100 categories) that a full crawl is the cheap
        operation here, so scan and watch both just do this.
        """
        self._failed_files = []
        self._login()
        categories = self._categories()
        self.log(f"Found {len(categories)} product categories.", "INFO")

        priority_cats, other_cats = self._split_priority(categories)
        if self.priority:
            self.log(f"Priority: {len(priority_cats)} categor{'y' if len(priority_cats) == 1 else 'ies'} "
                      f"matching {self.priority} will be done first.", "INFO")

        new_groups = []
        for cats, label in ((priority_cats, "priority"), (other_cats, "remaining")):
            if not cats or self._stopped():
                continue
            new_groups.extend(self._run_phase(cats, "current", label))
            if self.include_archive and not self._stopped():
                new_groups.extend(self._run_phase(cats, "archive", label))

        self.manifest.mark_full_scan()
        self.manifest.save()
        self.log(f"Scan complete: {len(new_groups)} new release group(s) downloaded.", "SUCCESS")
        if self._failed_files:
            self.log(f"{len(self._failed_files)} file(s) failed after all retries:", "ERROR")
            for f in self._failed_files:
                self.log(f"  [{f['group']}] {f['filename']}: {f['error']}", "ERROR")
        else:
            self.log("No failed files.", "SUCCESS")
        return new_groups

    def watch_once(self):
        """Same crawl as full_scan (see note above on why); kept as a
        separate name so the CLI/systemd-timer shape matches the MikroTik
        tool for consistency.
        """
        result = self.full_scan()
        self.manifest.mark_watch_check()
        self.manifest.save()
        return result

    # -- per-category work -----------------------------------------------
    def _run_phase(self, categories, phase: str, label: str = ""):
        tag = f"{label}/{phase}" if label else phase
        total = len(categories)
        self.log(f"Starting '{tag}' pass across {total} categories...", "INFO")
        new_groups = []
        done = 0
        with ThreadPoolExecutor(max_workers=self.category_workers) as ex:
            futures = {ex.submit(self._process_category, c, phase): c for c in categories}
            for fut in as_completed(futures):
                cat = futures[fut]
                if self._stopped():
                    continue
                try:
                    new_groups.extend(fut.result())
                except Exception as e:
                    self.log(f"Failed to process category {cat.name} ({phase}): {e}", "ERROR")
                done += 1
                stats = self.manifest.data["stats"]
                self.log(
                    f"[{tag}] {done}/{total} categories done | "
                    f"files: {stats['files_downloaded']} ok, {stats['files_failed']} failed, {stats['files_skipped']} skipped | "
                    f"{format_size(stats['bytes_downloaded'])} downloaded so far",
                    "PROGRESS",
                )
        return new_groups

    def _process_category(self, category: discovery.Category, phase: str):
        if self._stopped():
            return []
        if phase == "current":
            releases = self._discover_with_reauth(discovery.discover_current_releases, category)
        else:
            releases = self._discover_with_reauth(discovery.discover_archive_releases, category)

        new_groups = []
        for release in releases:
            if self._stopped():
                break
            group_id = f"{category.slug}/{release.release_id}"
            is_new = not self.manifest.has_group(group_id)
            self.manifest.record_group(group_id)
            self._download_release(category, release, group_id)
            if is_new:
                new_groups.append(group_id)
        self.manifest.save()
        return new_groups

    def _download_release(self, category: discovery.Category, release: discovery.Release, group_id: str):
        subdir = self.output_dir / category.slug / _sanitize(f"{release.release_id}_{release.title}")
        with ThreadPoolExecutor(max_workers=self.download_workers) as ex:
            futures = [
                ex.submit(self._download_file, group_id, subdir, f)
                for f in release.files
            ]
            for fut in as_completed(futures):
                if self._stopped():
                    break
                try:
                    fut.result()
                except Exception as e:
                    self.log(f"Unexpected download error: {e}", "ERROR")

    def _download_file(self, group_id: str, subdir: Path, file_meta: discovery.ReleaseFile,
                        retry: int = 0, rate_limit_retry: int = 0, max_rate_limit_retries: int = 12) -> bool:
        filename = _sanitize(file_meta.filename)
        out_path = subdir / filename

        if out_path.exists() and out_path.stat().st_size > 0:
            if not self.manifest.has_file(group_id, filename):
                size = out_path.stat().st_size
                rel_path = str(out_path.relative_to(self.output_dir))
                self.manifest.record_file(group_id, filename, size, rel_path, file_meta.url)
            self._emit("stat", key="files_skipped", amount=1)
            self.manifest.bump_stat("files_skipped")
            return True

        self._backoff.wait_if_paused()
        try:
            resp = self.session.get(file_meta.url, timeout=60, stream=True)
            if resp.status_code == 404:
                return False
            if resp.status_code == 429:
                # A generous, separate retry budget from the generic
                # connection-error one below - a 429 means "you'll succeed,
                # just not yet", not "something's actually broken". Every
                # thread shares one backoff clock (RateLimitBackoff), so
                # this pauses the whole crawl together instead of each
                # thread retrying on its own schedule and immediately
                # re-triggering the same limit.
                if rate_limit_retry >= max_rate_limit_retries:
                    raise requests.HTTPError(f"429 Too Many Requests for {filename} (gave up after {max_rate_limit_retries} backoffs)")
                delay = self._backoff.trigger(resp.headers.get("Retry-After"))
                self.log(f"Rate limited (429) on {filename}, attempt {rate_limit_retry + 1}/{max_rate_limit_retries} - "
                         f"backing off {delay:.0f}s...", "WARNING")
                self._backoff.wait_if_paused()
                return self._download_file(group_id, subdir, file_meta, retry, rate_limit_retry + 1, max_rate_limit_retries)
            if "/login" in resp.url:
                # Session expired mid-crawl (long historical crawls can outlast
                # a session lifetime) - re-auth once and retry this file.
                self.log("Session expired mid-download, re-authenticating...", "WARNING")
                auth.login(self.session, self.email, self.password)
                auth.save_session(self.session, self.session_cache)
                resp = self.session.get(file_meta.url, timeout=60, stream=True)
            resp.raise_for_status()

            out_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if self._rate_limiter:
                        self._rate_limiter.consume(len(chunk))
                    f.write(chunk)
                    downloaded += len(chunk)
                    if self._stopped():
                        break

            rel_path = str(out_path.relative_to(self.output_dir))
            self.manifest.record_file(group_id, filename, downloaded, rel_path, file_meta.url)
            self.manifest.bump_stat("files_downloaded")
            self.manifest.bump_stat("bytes_downloaded", downloaded)
            self._emit("stat", key="files_downloaded", amount=1)
            self._emit("stat", key="bytes_downloaded", amount=downloaded)
            self.log(f"[{group_id}] {filename} ({downloaded / 1024 / 1024:.2f} MB)", "SUCCESS")
            return True
        except requests.RequestException as e:
            if retry < self.max_retries:
                backoff = 2 ** retry
                self.log(f"Retry {retry + 1}/{self.max_retries} for {filename}: {e} (sleeping {backoff}s)", "WARNING")
                time.sleep(backoff)
                return self._download_file(group_id, subdir, file_meta, retry + 1)
            self.log(f"Failed: {filename}: {e}", "ERROR")
            self.manifest.bump_stat("files_failed")
            self._emit("stat", key="files_failed", amount=1)
            with self._progress_lock:
                self._failed_files.append({"group": group_id, "filename": filename, "error": str(e)})
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            return False
