#!/usr/bin/env python3
"""Monthly usage report for a store.

Usage:
    python scripts/report.py <store_id> --month 2026-09
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.reporting import generate_monthly_report, render_markdown  # noqa: E402
from app.store_config import StoreConfigError, load_store_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id")
    parser.add_argument("--month", required=True, help="YYYY-MM")
    args = parser.parse_args()

    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", args.month):
        print(f"error: --month must be in YYYY-MM format, got {args.month!r}", file=sys.stderr)
        return 1

    app = create_app()
    with app.app_context():
        app_config = app.extensions["app_config"]
        try:
            load_store_config(args.store_id, stores_dir=app_config.stores_dir)
        except StoreConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if not app_config.anthropic_api_key:
            print("error: ANTHROPIC_API_KEY is not set (needed to categorise topics)", file=sys.stderr)
            return 1

        report = generate_monthly_report(db.session, app_config, args.store_id, args.month)
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
