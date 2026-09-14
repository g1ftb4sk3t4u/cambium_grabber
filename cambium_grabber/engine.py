"""Core crawl/download engine: log in, walk every product category, pull
every file in every release, skip what's already on disk. Modeled on the
MikroTik grabber's engine (same retry/backoff, same atomic manifest, same
skip-if-already-downloaded logic) but discovery is a real link crawl instead
of brute-force version guessing, and every request needs an authenticated
session.

Concurrency defaults are deliberately modest (see DEFAULT_* below) - this
is hitting a vendor portal as a logged-in person, not an anonymous public
CDN, so it's worth not hammering it even though nothing here is rate-limited
server-side as far as we've seen.
"""

import re
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
DEFAULT_CATEGORY_WORKERS = 4
DEFAULT_DOWNLOAD_WORKERS = 4

_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize(name: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", name).strip()


class Engine:
    def __init__(self, output_dir: str, email: str, password: str,
                 category_workers: int = DEFAULT_CATEGORY_WORKERS,
                 download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
                 max_retries: int = 3, include_archive: bool = True,
                 category_filter=None, priority=None, on_event=None, stop_flag=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.password = password
        self.category_workers = max(1, category_workers)
        self.download_workers = max(1, download_workers)
        self.max_retries = max_retries
        self.include_archive = include_archive
        self.category_filter = category_filter  # optional set of slugs to restrict to
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
        self.log(f"Starting '{tag}' pass across {len(categories)} categories...", "INFO")
        new_groups = []
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
        return new_groups

    def _process_category(self, category: discovery.Category, phase: str):
        if self._stopped():
            return []
        if phase == "current":
            releases = discovery.discover_current_releases(self.session, category)
        else:
            releases = discovery.discover_archive_releases(self.session, category)

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

    def _download_file(self, group_id: str, subdir: Path, file_meta: discovery.ReleaseFile, retry: int = 0) -> bool:
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

        try:
            resp = self.session.get(file_meta.url, timeout=60, stream=True)
            if resp.status_code == 404:
                return False
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
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            return False
