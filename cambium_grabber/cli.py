"""Headless CLI. Same shape as the MikroTik grabber's CLI (scan/watch,
--once for cron/systemd) plus a `login` subcommand for verifying credentials
and priming the cached session before the first scheduled run.

Credentials: prefer the CAMBIUM_EMAIL / CAMBIUM_PASSWORD environment
variables (systemd's EnvironmentFile= is the intended way to supply these -
see deploy/centos) over --email/--password, since command-line args are
visible to anyone on the box via `ps`.
"""

import argparse
import getpass
import os
import sys

from .engine import Engine


def _resolve_credentials(args):
    email = args.email or os.environ.get("CAMBIUM_EMAIL")
    password = args.password or os.environ.get("CAMBIUM_PASSWORD")
    if not email:
        email = input("Cambium account email: ").strip()
    if not password:
        password = getpass.getpass("Cambium account password: ")
    if not email or not password:
        print("Both an email and password are required (CAMBIUM_EMAIL / CAMBIUM_PASSWORD or --email/--password).", file=sys.stderr)
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
        on_event=_console_event_handler(args.verbose),
    )


def cmd_login(args):
    email, password = _resolve_credentials(args)
    engine = Engine(output_dir=args.output, email=email, password=password,
                     on_event=_console_event_handler(True))
    engine._login()
    print(f"Logged in OK. Session cached at {engine.session_cache}")


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
    common.add_argument("--email", default=None, help="Cambium account email (or CAMBIUM_EMAIL env var)")
    common.add_argument("--password", default=None, help="Cambium account password (or CAMBIUM_PASSWORD env var - preferred)")
    common.add_argument("--dl-workers", type=int, default=4, help="Concurrent download workers")
    common.add_argument("--category-workers", type=int, default=4, help="Concurrent category-crawl workers")
    common.add_argument("--retries", type=int, default=3, help="Max retries per file")
    common.add_argument("--categories", default=None, help="Comma-separated category slugs to restrict to (default: all)")
    common.add_argument("--priority", default=None,
                         help="Comma-separated group/category names to fully finish (current+archive) before anything else, "
                              "e.g. --priority ePMP,PTP (matched case-insensitively against group, category name, and slug)")
    common.add_argument("--no-archive", action="store_true", help="Skip each product's Archive tab, current releases only")
    common.add_argument("-v", "--verbose", action="store_true", help="Show INFO-level logs, not just findings/errors")

    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", parents=[common], help="Verify credentials and cache a session")
    p_login.set_defaults(func=cmd_login)

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
