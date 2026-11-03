#!/usr/bin/env python3
"""Run (or re-run) live onboarding for a client store: scrape its public
storefront and draft its knowledge base with Claude (app/live_onboarding.py
-> app/demo_kb.py), writing stores/<store_id>.yaml as a pending_review
live draft.

Normally run AUTOMATICALLY, as a detached background process spawned by
/oauth/callback right after it saves the OAuth token (see
app/routes/oauth.py's spawn_provisioning_job) — the scrape + Claude call
can take well over gunicorn's 60s worker timeout, so the callback hands
off to this script instead of running it inline and risking the worker
being killed mid-request (which is what used to hang the install).

Also safe and useful to run BY HAND:
  - to retry after a crash, or a knowledge-base draft that failed/was
    skipped (e.g. the cost cap was hit — raise it with --max-cost and
    re-run)
  - after a merchant reopens the install link to refresh scopes — this
    is idempotent: a store already reviewed (live_status != pending_review)
    is left completely untouched, never re-drafted or reset to pending.

Usage:
    python scripts/provision_store.py tillbird-test
    python scripts/provision_store.py tillbird-test --max-cost 2.00
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import anthropic  # noqa: E402

from app import create_app  # noqa: E402
from app.anthropic_usage import UsageTrackingClient  # noqa: E402
from app.extensions import db  # noqa: E402
from app.live_onboarding import DEFAULT_PLAN, provision_live_store  # noqa: E402
from app.models import ShopifyAppCredential, ShopifyOAuthCredential  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id", help="store_id to provision — must already have a row in the OAuth credentials table (i.e. already installed)")
    parser.add_argument("--max-cost", type=float, default=None, help="override $LIVE_ONBOARDING_MAX_COST for this run")
    args = parser.parse_args()

    app = create_app()
    app_config = app.extensions["app_config"]
    if args.max_cost is not None:
        app_config.live_onboarding_max_cost = args.max_cost

    with app.app_context():
        credential = db.session.get(ShopifyOAuthCredential, args.store_id)
        if credential is None:
            print(f"error: no Shopify OAuth credential stored for store_id {args.store_id!r} — install first", file=sys.stderr)
            return 1

        app_credential = db.session.get(ShopifyAppCredential, args.store_id)
        plan = (app_credential.plan if app_credential else None) or DEFAULT_PLAN

        tracking_client = UsageTrackingClient(anthropic.Anthropic(api_key=app_config.anthropic_api_key)) if app_config.anthropic_api_key else None

        result = provision_live_store(
            credential.shop_domain,
            plan,
            app_config,
            stores_dir=app_config.stores_dir,
            client=tracking_client,
        )

    print(f"store_id: {result['store_id']}")
    print(f"yaml: {result['yaml_path']}")
    print(f"kb_status: {result['kb_status']}")
    cost = tracking_client.total_cost() if tracking_client else 0.0
    print(f"cost: ${cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
