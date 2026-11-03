#!/usr/bin/env python3
"""One-shot database schema initialization.

Run this once, strictly before gunicorn's worker processes exist (see
docker-entrypoint.sh) — NOT by gunicorn itself. create_app() calls
db.create_all() as part of normal startup, and gunicorn's default (no
--preload) forks workers that each import wsgi:app independently; if the
database is fresh, every worker's create_all() races the others' and all
but one crash with "table X already exists" ("Worker failed to boot"),
recovering only because the next restart finds the tables already there.

Running schema creation here, in its own process that exits before gunicorn
starts, removes the concurrency entirely: by the time workers fork, the
schema already exists and their own create_all() call is a no-op.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402


def main() -> int:
    create_app()  # building the app runs create_all_tables_safely() as a side effect
    print("Database schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
