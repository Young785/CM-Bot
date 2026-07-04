#!/usr/bin/env python3
"""Send a CM Bot report email immediately (CLI)."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cmbot.reports.scheduler import ReportScheduler  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Send CM Bot email report")
    parser.add_argument(
        "--period",
        choices=("test", "daily", "weekly", "monthly"),
        default="test",
        help="Report period (default: test)",
    )
    args = parser.parse_args()

    sched = ReportScheduler(get_bot_processes=lambda: {}, get_bot_schedulers=lambda: {})
    result = sched.send_now(args.period)
    if result.get("success"):
        print(f"Sent to {result.get('to')}: {result.get('subject')}")
        return 0
    print(f"Failed: {result.get('error')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
