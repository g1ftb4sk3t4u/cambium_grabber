"""Headless CLI. Same shape as the MikroTik grabber's CLI (scan/watch,
--once for cron/systemd) plus a `login` subcommand for verifying credentials
and priming the cached session before the first scheduled run.

Credentials, in priority order: --email/--password flags, then the
CAMBIUM_EMAIL/CAMBIUM_PASSWORD environment variables (systemd's
EnvironmentFile= is how deploy/centos supplies these), then a local
credentials.env file (copy credentials.env.example - each person filling in
their own account, or a shared non-personal one your team sets up for this,
rather than one baked into the tool), then an interactive prompt as a last
resort.
"""

import argparse
import getpass
import os
import sys

from .engine import DEFAULT_CATEGORY_WORKERS, DEFAULT_DOWNLOAD_WORKERS, Engine, format_size


def _load_env_file(path):
    """Parse a simple KEY=VALUE file (# comments, blank lines ignored).
    Deliberately not python-dotenv - this is the one thing it needs, no
    reason to add a dependency for it.
    """
    values = {}
    if not path or not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _resolve_credentials(args):
    env_file = _load_env_file(getattr(args, "env_file", None))
    email = args.email or os.environ.get("CAMBIUM_EMAIL") or env_file.get("CAMBIUM_EMAIL")
    password = args.password or os.environ.get("CAMBIUM_PASSWORD") or env_file.get("CAMBIUM_PASSWORD")

    # If stdout isn't a real terminal (redirected to a log file, running
    # under systemd, etc.), input()/getpass() would block forever waiting
    # on a prompt nobody can see - looking exactly like the tool being
    # stuck, with nothing printed anywhere. Fail loudly instead.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if (not email or not password) and not interactive:
        print("Missing credentials and no terminal to prompt on (output is redirected/non-interactive).", file=sys.stderr)
        print(f"Checked: --email/--password flags, CAMBIUM_EMAIL/CAMBIUM_PASSWORD env vars, "
              f"and {getattr(args, 'env_file', 'credentials.env')} (copy credentials.env.example and fill it in).", file=sys.stderr)
        sys.exit(1)

    if not email:
        email = input("Cambium account email: ").strip()
    if not password:
        password = getpass.getpass("Cambium account password: ")
    if not email or not password:
        print("Both an email and password are required "
              "(--email/--password, CAMBIUM_EMAIL/CAMBIUM_PASSWORD, or a credentials.env file - see credentials.env.example).",
              file=sys.stderr)
        sys.exit(1)
    return email, password


def _console_event_handler(verbose: bool):
    def handler(evt):
        if evt["type"] == "log":
            if evt["level"] == "INFO" and not verbose:
                return
            print(f"[{evt['level']}] {evt['message']}", flush=True)
    return handler


def _build_engine(args) -> Engine:
    email, password = _resolve_credentials(args)
    category_filter = set(args.categories.split(",")) if args.categories else None
    priority = args.priority.split(",") if args.priority else None
    return Engine(
        output_dir=args.output,
        email=email,
        password=password,
        category_workers=args.category_workers,
        download_workers=args.dl_workers,
        max_retries=args.retries,
        include_archive=not args.no_archive,
        category_filter=category_filter,
        priority=priority,
        max_mbps=args.max_mbps,
        deferred_retry_passes=args.retry_passes,
        deferred_retry_pause=args.retry_pause,
        on_event=_console_event_handler(args.verbose),
    )


def cmd_login(args):
    email, password = _resolve_credentials(args)
    engine = Engine(output_dir=args.output, email=email, password=password,
                     on_event=_console_event_handler(True))
    engine._login()
    print(f"Logged in OK. Session cached at {engine.session_cache}")


def cmd_estimate(args):
    engine = _build_engine(args)
    totals = engine.estimate()
    print(f"\n{totals['categories']} product categories crawled.\n")
    print(f"{'Group':<28}{'Files':>8}{'Size':>12}{'New files':>12}{'New size':>12}")
    for group, s in sorted(totals["by_group"].items()):
        print(f"{group:<28}{s['files']:>8}{format_size(s['bytes']):>12}{s['new_files']:>12}{format_size(s['new_bytes']):>12}")
    print("-" * 72)
    print(f"{'TOTAL':<28}{totals['files']:>8}{format_size(totals['bytes']):>12}{totals['new_files']:>12}{format_size(totals['new_bytes']):>12}")
    print(f"\nAlready on disk: {totals['files'] - totals['new_files']} files, {format_size(totals['bytes'] - totals['new_bytes'])}")
    print(f"Would download:  {totals['new_files']} files, {format_size(totals['new_bytes'])}")


def cmd_scan(args):
    engine = _build_engine(args)
    new_groups = engine.full_scan()
    print(f"Scan complete. {len(new_groups)} new release group(s).")


def cmd_watch(args):
    engine = _build_engine(args)
    if args.once:
        new_groups = engine.watch_once()
        print(f"Watch check complete. {len(new_groups)} new release group(s).")
    else:
        import time
        try:
            while True:
                engine.watch_once()
                time.sleep(max(60.0, args.interval_hours * 3600))
        except KeyboardInterrupt:
            print("Stopped.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium-grabber",
        description="Crawl the Cambium Networks support portal and mirror firmware/software/MIBs/docs locally.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output", default="./cambium_archive", help="Archive output directory")
    common.add_argument("--email", default=None, help="Cambium account email (or CAMBIUM_EMAIL env var / credentials.env)")
    common.add_argument("--password", default=None, help="Cambium account password (or CAMBIUM_PASSWORD env var / credentials.env - preferred over this flag)")
    common.add_argument("--env-file", default="credentials.env",
                         help="Path to a KEY=VALUE credentials file (copy credentials.env.example). Default: ./credentials.env")
    common.add_argument("--dl-workers", type=int, default=DEFAULT_DOWNLOAD_WORKERS, help="Concurrent download workers")
    common.add_argument("--category-workers", type=int, default=DEFAULT_CATEGORY_WORKERS, help="Concurrent category-crawl workers")
    common.add_argument("--retries", type=int, default=3, help="Max retries per file")
    common.add_argument("--categories", default=None, help="Comma-separated category slugs to restrict to (default: all)")
    common.add_argument("--priority", default=None,
                         help="Comma-separated group/category names to fully finish (current+archive) before anything else, "
                              "e.g. --priority ePMP,PTP (matched case-insensitively against group, category name, and slug)")
    common.add_argument("--no-archive", action="store_true", help="Skip each product's Archive tab, current releases only")
    common.add_argument("--max-mbps", type=float, default=None,
                         help="Cap aggregate download speed in megabits/sec across all workers combined (default: unlimited - "
                              "nothing currently stops this from saturating the connection, so set this on a shared/limited link)")
    common.add_argument("--retry-passes", type=int, default=3,
                         help="How many patient cleanup passes to make over rate-limited (429) files after the main crawl finishes "
                              "(default: 3). A pass with nothing deferred is skipped instantly, so this costs nothing when unneeded.")
    common.add_argument("--retry-pause", type=float, default=300.0,
                         help="Seconds to wait before each retry pass after the first (default: 300 = 5 minutes) - "
                              "a real cooldown for the server, not just the per-request backoff.")
    common.add_argument("-v", "--verbose", action="store_true", help="Show INFO-level logs, not just findings/errors")

    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", parents=[common], help="Verify credentials and cache a session")
    p_login.set_defaults(func=cmd_login)

    p_estimate = sub.add_parser("estimate", parents=[common],
                                 help="Discovery-only dry run: report file counts/sizes without downloading anything")
    p_estimate.set_defaults(func=cmd_estimate)

    p_scan = sub.add_parser("scan", parents=[common], help="Full crawl of every product category")
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser("watch", parents=[common], help="Check for new releases (same crawl - the catalog is small enough it's always cheap)")
    p_watch.add_argument("--interval-hours", type=float, default=24.0, help="Hours between checks (default: 24, i.e. daily)")
    p_watch.add_argument("--once", action="store_true", help="Run a single check-and-download pass then exit (good for cron/systemd timers)")
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
