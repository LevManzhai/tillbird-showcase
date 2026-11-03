#!/usr/bin/env python3
"""Run ~40 scripted customer messages against a store and flag suspected failures.

Prints every reply and which tools were called; heuristic flags (see
app/evaluation.py) are a starting point for review, not a verdict — always
read flagged (and a sample of unflagged) replies yourself.

Usage:
    python scripts/eval.py <store_id>
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.config import AppConfig  # noqa: E402
from app.evaluation import run_eval  # noqa: E402
from app.store_config import StoreConfigError, load_store_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id")
    parser.add_argument("--stores-dir", default="stores")
    args = parser.parse_args()

    app_config = AppConfig()
    try:
        store_config = load_store_config(args.store_id, stores_dir=args.stores_dir)
    except StoreConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not app_config.anthropic_api_key:
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1

    print(f"Running eval against {args.store_id!r} ({store_config.mode} mode) ...\n")
    results = run_eval(app_config, store_config)

    for i, r in enumerate(results, 1):
        marker = "⚑ FLAGGED" if r.flagged else "ok"
        print(f"[{i}/{len(results)}] ({r.category}) [{marker}]")
        print(f"  > {r.message}")
        print(f"  < {r.reply}")
        print(f"  tools called: {', '.join(r.tool_names) or '(none)'}")
        if r.flagged:
            for issue in r.issues:
                print(f"  issue: {issue}")
        print()

    flagged = [r for r in results if r.flagged]
    print("=" * 60)
    print(f"TOTAL: {len(results)}   FLAGGED: {len(flagged)}")
    if flagged:
        print("\nFlagged cases:")
        for r in flagged:
            print(f"  - [{r.category}] {r.message!r}: {'; '.join(r.issues)}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
