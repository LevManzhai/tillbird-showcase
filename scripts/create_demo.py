#!/usr/bin/env python3
"""Generate a sales demo store config from a public Shopify storefront.

Fetches public product data and policy pages, drafts a knowledge base with
Claude (never inventing anything not actually stated on the site), and
writes stores/demo-<random-slug>.yaml + stores/demo-<random-slug>.products.json.

Usage:
    python scripts/create_demo.py https://emberhome.uk
"""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import yaml  # noqa: E402

from app.catalog import pick_demo_order_line_items  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.demo_kb import StoreProvisioningError, scrape_and_draft_store  # noqa: E402
from app.scraping import ScrapeError  # noqa: E402

DEFAULT_FORBIDDEN = [
    "Never promise a specific refund amount or approve a refund directly.",
    "Never offer a discount code or promise compensation.",
    "Never confirm or deny stock levels beyond what search_products returns.",
]
DEFAULT_GREETING = "Hi! How can I help you today?"
DEFAULT_HINT_TEXT = "Questions about an order or product? Ask me anything."


class DemoBuildError(Exception):
    """Raised for conditions build_demo() wants the caller to stop and report
    (password-protected store, not a Shopify store, no Anthropic key, the
    knowledge-base draft call failing) — callers catch this alongside
    ScrapeError (raised lower down by normalise_base_url) to handle both
    with one except clause.
    """


def generate_slug() -> str:
    # Unguessable by design (spec: "/demo/<random-slug>, not the store name").
    return "demo-" + secrets.token_urlsafe(6).lower().replace("_", "").replace("-", "")[:8]


def build_demo(store_url: str, stores_dir: Path, app_config: AppConfig, store_id: str | None = None) -> dict:
    """Scrape store_url, draft its knowledge base, and write
    stores/<store_id>.yaml + .products.json.

    store_id defaults to a fresh random slug if not given — batch_demos.py
    passes one in explicitly so it knows the demo URL before generation
    finishes (useful for logging progress across many stores).

    Returns a dict with the structured pieces a caller might want beyond
    what's on disk: store_id, store_name, host, products (list[ProductResult]),
    policy_texts (dict), draft (dict, including 'missing'), yaml_path,
    products_path. Raises ScrapeError or DemoBuildError on anything that
    should stop demo creation for this store — callers decide how to report
    that (main() below prints + exits; batch_demos.py logs and moves on to
    the next lead).
    """
    try:
        provisioned = scrape_and_draft_store(store_url, app_config)
    except StoreProvisioningError as exc:
        raise DemoBuildError(str(exc)) from exc

    base_url = provisioned["base_url"]
    host = provisioned["host"]
    shop_name = provisioned["shop_name"]
    brand_color = provisioned["brand_color"]
    products = provisioned["products"]
    policy_texts = provisioned["policy_texts"]
    draft = provisioned["draft"]

    store_id = store_id or generate_slug()

    # A re-run with --store-id regenerates this demo's yaml from scratch —
    # carry over any manual override a human set by hand (scripts/set_override.py)
    # rather than silently wiping it; a scrape must never overwrite one.
    existing_path = Path(stores_dir) / f"{store_id}.yaml"
    existing_overrides = []
    if existing_path.is_file():
        existing_raw = yaml.safe_load(existing_path.read_text(encoding="utf-8")) or {}
        existing_overrides = existing_raw.get("overrides") or []

    store_config = {
        "store_id": store_id,
        "store_name": shop_name,
        "shop_domain": host,  # demo mode never calls the Shopify Admin API
        "public_domain": host,
        "support_email": f"demo@{host}",  # placeholder — see printed summary note
        "tone": draft.get("tone") or "Friendly and helpful.",
        "brand_color": brand_color,
        "greeting": DEFAULT_GREETING,
        "hint_text": DEFAULT_HINT_TEXT,
        "privacy_policy_url": f"{base_url}/policies/privacy-policy" if "privacy" in policy_texts else None,
        "allowed_origins": [],
        "knowledge_base": {
            "shipping": draft.get("shipping", ""),
            "returns": draft.get("returns", ""),
            "sizing": draft.get("sizing", ""),
            "brand_claims": draft.get("brand_claims", ""),
            "promotions": draft.get("promotions", ""),
            "faq": draft.get("faq", []),
        },
        "forbidden": DEFAULT_FORBIDDEN,
        "mode": "demo",
        "demo_created_at": datetime.now(timezone.utc).isoformat(),
        "demo_order_line_items": pick_demo_order_line_items([p.to_dict() for p in products]) or None,
        "overrides": existing_overrides,
    }

    stores_dir = Path(stores_dir)
    stores_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = stores_dir / f"{store_id}.yaml"
    products_path = stores_dir / f"{store_id}.products.json"

    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(store_config, f, sort_keys=False, allow_unicode=True)
    with products_path.open("w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in products], f, indent=2, ensure_ascii=False)

    return {
        "store_id": store_id,
        "store_name": shop_name,
        "host": host,
        "base_url": base_url,
        "products": products,
        "policy_texts": policy_texts,
        "draft": draft,
        "yaml_path": yaml_path,
        "products_path": products_path,
    }


def public_demo_url(app_config: AppConfig, store_id: str, public_app_url: str | None = None) -> str:
    base = public_app_url or app_config.public_base_url or f"http://localhost:{os.environ.get('PORT', '8000')}"
    return f"{base}/demo/{store_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_url", help="Public URL of the Shopify store, e.g. https://example.com")
    parser.add_argument("--stores-dir", default="stores")
    parser.add_argument(
        "--store-id",
        default=None,
        help="Overwrite an existing demo IN PLACE (same store_id, same demo URL) instead of minting a "
        "new slug — use this to regenerate a demo after a generator fix without changing its link.",
    )
    parser.add_argument(
        "--public-app-url",
        default=None,
        help="Override the base URL printed for the demo link (defaults to $PUBLIC_BASE_URL, then localhost)",
    )
    args = parser.parse_args()

    app_config = AppConfig()
    print(f"Fetching {args.store_url} ...")
    try:
        result = build_demo(args.store_url, Path(args.stores_dir), app_config, store_id=args.store_id)
    except (ScrapeError, DemoBuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    demo_url = public_demo_url(app_config, result["store_id"], args.public_app_url)
    missing = result["draft"].get("missing") or []

    print()
    print("=" * 60)
    print(f"Store name:     {result['store_name']}")
    print(f"Products found: {len(result['products'])}")
    print(f"Policies found: {', '.join(sorted(result['policy_texts'])) or '(none)'}")
    print(f"Missing/TODO:   {'; '.join(missing) if missing else '(nothing flagged)'}")
    print(f"support_email is a placeholder (demo@{result['host']}) — we don't have access to their real inbox.")
    print(f"Config written: {result['yaml_path']}")
    print(f"Products file:  {result['products_path']}")
    print(f"Demo URL:       {demo_url}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
