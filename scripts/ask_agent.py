#!/usr/bin/env python3
"""Ask the real agent loop a question directly against a store's config,
bypassing the HTTP API entirely — no rate-limit check, and nothing written
to the messages table. Useful for testing/reproducing an issue against a
specific store (e.g. one whose demo has hit its own daily/lifetime usage
cap from earlier testing) without touching its real usage history.

Calls app.agent.loop.run_agent_turn directly — the same production code
path app/routes/chat.py uses for a real request, just without the Flask
route, the rate-limit checks in that route, or the DB write it does after.

Usage:
    python scripts/ask_agent.py demo-2cyactv2 "If I don't like the scent, can I get a refund?"
    python scripts/ask_agent.py demo-2cyactv2 "..." --repeat 10
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.agent.loop import run_agent_turn  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.store_config import StoreConfigError, load_store_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id")
    parser.add_argument("question")
    parser.add_argument("--stores-dir", default="stores")
    parser.add_argument("--repeat", type=int, default=1, help="ask the same question this many times (fresh turn each time)")
    args = parser.parse_args()

    app_config = AppConfig()
    if not app_config.anthropic_api_key:
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1

    try:
        store_config = load_store_config(args.store_id, stores_dir=args.stores_dir)
    except StoreConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for i in range(1, args.repeat + 1):
        result = run_agent_turn(app_config, store_config, [], args.question)
        prefix = f"[{i}/{args.repeat}] " if args.repeat > 1 else ""
        print(f"{prefix}{result.reply}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
