#!/usr/bin/env python3
"""Entry point. `python run.py --help` for all subcommands.

Examples:
  python run.py login                              # verify credentials, cache a session
  python run.py scan --output ./cambium_archive     # full crawl of every product category
  python run.py watch --once                        # single daily-style check, good for cron/systemd
"""

from cambium_grabber.cli import main

if __name__ == "__main__":
    main()
