"""Turn a successful OAuth install (app/routes/oauth.py) into a live store
— reuses app.demo_kb.scrape_and_draft_store (the exact same scrape +
knowledge-base-draft Claude call a sales demo uses) against the shop's own
public storefront, then writes a LIVE-mode stores/<store_id>.yaml instead
of a demo one.

The resulting store is never answering real customers immediately: it's
written with live_status: "pending_review" (see app.store_config
.StoreConfig), which app/routes/chat.py refuses to serve until a human
runs scripts/approve_live_store.py — the auto-draft is a head start, not
something sent straight to production.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.demo_kb import StoreProvisioningError, scrape_and_draft_store
from app.origin_discovery import compute_discovered_origins, merge_allowed_origins
from app.scraping import ScrapeError
from app.shopify_client import build_oauth_client
from app.store_config import PLAN_DEFAULT_CONVERSATION_LIMITS

DEFAULT_PLAN = "starter"
DEFAULT_FORBIDDEN = [
    "Never promise a specific refund amount or approve a refund directly.",
    "Never offer a discount code or promise compensation.",
    "Never confirm or deny stock levels beyond what search_products returns.",
]
DEFAULT_GREETING = "Hi! How can I help you today?"
DEFAULT_HINT_TEXT = "Questions about an order or product? Ask me anything."

# Flat per-store cost assumption for the pre-flight check before the
# automatic knowledge-base draft — same approach as
# scripts.audit_store.ASSUMED_COST_PER_STORE_USD. The install itself
# always succeeds even if this trips; only the KB draft is skipped (left
# blank, for a manual scripts/create_demo.py-style follow-up run).
ASSUMED_ONBOARDING_COST_USD = 0.15

_SHOP_SUFFIX_RE = re.compile(r"\.myshopify\.com$", re.IGNORECASE)
_NON_ID_CHARS_RE = re.compile(r"[^a-z0-9-]+")


def store_id_for_shop(shop_domain: str) -> str:
    """A stable, human-readable store_id derived from the shop's own
    subdomain (e.g. "acme-fashion.myshopify.com" -> "acme-fashion") — NOT
    a random slug like scripts.create_demo.generate_slug(), so a client
    reinstalling after uninstalling lands on the same store_id (and the
    same stores/<id>.yaml) instead of silently forking into a duplicate.
    """
    base = _SHOP_SUFFIX_RE.sub("", shop_domain.lower())
    return _NON_ID_CHARS_RE.sub("-", base).strip("-") or "store"


def provision_live_store(
    shop_domain: str,
    plan: str | None,
    app_config,
    stores_dir: Path | str = "stores",
    client=None,
    shopify_client=None,
) -> dict:
    """Writes stores/<store_id>.yaml for a newly-installed client,
    mode: live, live_status: pending_review. Always succeeds (the install
    itself must never fail just because the auto-draft did) — if scraping
    the public storefront fails, or the cost cap is hit, or drafting
    itself errors, falls back to a minimal YAML with an empty knowledge
    base and kb_status recording why, for a manual follow-up. Same
    tolerance for allowed_origins auto-discovery (see origins_status in
    the return value) — a Shopify API hiccup here must never fail the
    install either.

    Idempotent and safe to re-run (scripts/provision_store.py does this,
    both as the background job /oauth/callback kicks off and by hand):
    if stores/<store_id>.yaml already exists with live_status something
    OTHER than "pending_review" (i.e. a human already reviewed it, or it
    was deliberately deactivated), this is a no-op that leaves the file
    untouched — re-running provisioning (e.g. a merchant reopening the
    install link to refresh scopes) must never silently overwrite an
    approved config or a knowledge base a human has since hand-edited.
    A still-"pending_review" file (nothing approved yet) is fine to
    re-draft from scratch, same as a fresh install.

    `client` is forwarded to scrape_and_draft_store/draft_knowledge_base
    — pass a app.anthropic_usage.UsageTrackingClient to report real cost.
    `shopify_client` overrides the ShopifyAdminClient used for
    allowed_origins discovery (tests inject a fake one here).

    Returns {"store_id", "yaml_path", "kb_status": "drafted" |
    "skipped_cost_cap" | "skipped_already_provisioned" | "failed: <reason>",
    "origins_status": "discovered" | "failed: <reason>"}.

    Raises ValueError for an unrecognised plan — fail loudly here rather
    than writing it into stores/<store_id>.yaml, where it would only
    surface later (and less clearly) as a StoreConfig validation error
    the next time anything tries to load that store.
    """
    if plan is not None and plan not in PLAN_DEFAULT_CONVERSATION_LIMITS:
        raise ValueError(f"plan must be one of {sorted(PLAN_DEFAULT_CONVERSATION_LIMITS)}, got {plan!r}")

    store_id = store_id_for_shop(shop_domain)
    store_url = f"https://{shop_domain}"
    stores_dir = Path(stores_dir)
    stores_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = stores_dir / f"{store_id}.yaml"

    existing_overrides = []
    existing_allowed_origins = []
    if yaml_path.exists():
        existing = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if existing.get("live_status") != "pending_review":
            print(f"live onboarding for {shop_domain}: already provisioned (live_status={existing.get('live_status')!r}) — leaving {yaml_path} untouched.")
            return {"store_id": store_id, "yaml_path": yaml_path, "kb_status": "skipped_already_provisioned"}
        # Still pending review (nothing approved yet), so a re-draft from
        # scratch below is fine — but a manual correction (scripts/set_override.py)
        # or an origin added by hand could in principle already be set even
        # before approval, and must never be silently wiped by this
        # re-draft either way.
        existing_overrides = existing.get("overrides") or []
        existing_allowed_origins = existing.get("allowed_origins") or []

    base_config = {
        "store_id": store_id,
        "store_name": shop_domain,  # placeholder until/unless drafting succeeds and overwrites it below
        "shop_domain": shop_domain,
        "public_domain": shop_domain,
        "support_email": f"support@{shop_domain}",  # placeholder — update once the client confirms their real inbox
        "tone": "Friendly and helpful.",
        "brand_color": "#1a1a2e",
        "greeting": DEFAULT_GREETING,
        "hint_text": DEFAULT_HINT_TEXT,
        "privacy_policy_url": None,
        "allowed_origins": existing_allowed_origins,
        "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "brand_claims": "", "promotions": "", "faq": []},
        "forbidden": DEFAULT_FORBIDDEN,
        "mode": "live",
        "plan": plan,
        "live_status": "pending_review",
        "live_onboarded_at": datetime.now(timezone.utc).isoformat(),
        "overrides": existing_overrides,
    }

    # allowed_origins auto-discovery (see app.origin_discovery) — only
    # ADDS to whatever's already there (existing_allowed_origins, set
    # above), never removes an origin added by hand. Independent of the
    # knowledge-base cost cap below (this is a Shopify API call, not an
    # Anthropic one) — a failure here is never fatal to the install.
    try:
        shop_client = shopify_client or build_oauth_client(store_id, shop_domain, app_config)
        domains = shop_client.get_shop_domains()
        discovered = compute_discovered_origins(domains["primary_host"], domains["myshopify_domain"])
        base_config["allowed_origins"] = merge_allowed_origins(existing_allowed_origins, discovered)
        origins_status = "discovered"
    except Exception as exc:
        print(f"live onboarding for {shop_domain}: couldn't auto-discover allowed_origins, leaving as-is: {exc}")
        origins_status = f"failed: {exc}"

    estimate = ASSUMED_ONBOARDING_COST_USD
    if estimate > app_config.live_onboarding_max_cost:
        print(f"live onboarding for {shop_domain}: estimated cost ${estimate:.2f} exceeds LIVE_ONBOARDING_MAX_COST ${app_config.live_onboarding_max_cost:.2f} — skipping the automatic knowledge-base draft.")
        kb_status = "skipped_cost_cap"
    else:
        print(f"live onboarding for {shop_domain}: estimated cost ${estimate:.2f} for the automatic knowledge-base draft.")
        try:
            provisioned = scrape_and_draft_store(store_url, app_config, client=client)
        except (ScrapeError, StoreProvisioningError) as exc:
            print(f"live onboarding for {shop_domain}: knowledge-base draft failed, continuing with an empty one: {exc}")
            kb_status = f"failed: {exc}"
        else:
            draft = provisioned["draft"]
            base_config["store_name"] = provisioned["shop_name"]
            base_config["brand_color"] = provisioned["brand_color"]
            base_config["tone"] = draft.get("tone") or base_config["tone"]
            base_config["knowledge_base"] = {
                "shipping": draft.get("shipping", ""),
                "returns": draft.get("returns", ""),
                "sizing": draft.get("sizing", ""),
                "brand_claims": draft.get("brand_claims", ""),
                "promotions": draft.get("promotions", ""),
                "faq": draft.get("faq", []),
            }
            if "privacy" in provisioned["policy_texts"]:
                base_config["privacy_policy_url"] = f"{provisioned['base_url']}/policies/privacy-policy"
            kb_status = "drafted"

    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(base_config, f, sort_keys=False, allow_unicode=True)

    return {"store_id": store_id, "yaml_path": yaml_path, "kb_status": kb_status, "origins_status": origins_status}
