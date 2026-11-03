#!/usr/bin/env python3
"""Ask a Tillbird demo a question over HTTP and print its reply — lets you
test a demo (scripts/remote_demo.sh) without opening a browser.

Sends the X-Tillbird-Internal-Test header (see app/routes/chat.py) when
INTERNAL_TEST_TOKEN is set in .env, so this doesn't eat into the demo's
own public usage cap — confirmed live, repeated manual testing this way
exhausted a real demo's daily cap before a prospect even opened the link.
Still works fine without the token configured, just subject to the normal
public caps like any other visitor.

Usage:
    python scripts/test_demo.py https://app.tillbird.co.uk/demo/demo-abc123 "Do you ship to Ireland?"
"""
import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import requests  # noqa: E402

TIMEOUT_SECONDS = 60
INTERNAL_TEST_HEADER = "X-Tillbird-Internal-Test"


class DemoQueryError(Exception):
    """Raised for anything main() should stop and report on stderr rather
    than crash on: a bad demo_url, a network failure, or a non-200/
    non-JSON/unexpected-shape response from /api/chat.
    """


def store_id_from_demo_url(demo_url: str) -> str:
    path = urlparse(demo_url).path.rstrip("/")
    store_id = path.rsplit("/", 1)[-1]
    if not store_id:
        raise DemoQueryError(f"could not extract a store_id from {demo_url!r}")
    return store_id


def ask_demo(demo_url: str, question: str, session_id: str | None = None) -> str:
    """POSTs question to the demo's /api/chat endpoint and returns the bot's reply text.

    session_id defaults to a fresh random id (a new conversation) — pass one in
    explicitly to continue an existing multi-turn conversation.
    """
    parsed = urlparse(demo_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    store_id = store_id_from_demo_url(demo_url)
    session_id = session_id or str(uuid.uuid4())

    headers = {}
    internal_test_token = os.environ.get("INTERNAL_TEST_TOKEN", "")
    if internal_test_token:
        headers[INTERNAL_TEST_HEADER] = internal_test_token

    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json={"store_id": store_id, "session_id": session_id, "message": question},
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise DemoQueryError(f"request to {demo_url} failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise DemoQueryError(f"non-JSON response ({response.status_code}): {response.text[:200]!r}") from exc

    if response.status_code != 200:
        raise DemoQueryError(f"demo returned HTTP {response.status_code}: {data}")
    if "reply" not in data:
        raise DemoQueryError(f"unexpected response shape (no 'reply' key): {data}")

    return data["reply"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("demo_url", help="e.g. https://app.tillbird.co.uk/demo/demo-abc123")
    parser.add_argument("question", help="the customer question to ask, e.g. 'Do you ship to Ireland?'")
    parser.add_argument("--session-id", default=None, help="reuse a session id for a multi-turn conversation")
    args = parser.parse_args()

    try:
        reply = ask_demo(args.demo_url, args.question, session_id=args.session_id)
    except DemoQueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
