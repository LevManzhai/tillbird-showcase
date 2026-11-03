import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Standard monthly conversation allowance per plan (see site/index.html's
# pricing section) — used only to default StoreConfig.monthly_conversation_limit
# when a plan is set but no explicit limit was given. Must match
# app.usage_limits.PLAN_LIMITS, which is what's actually enforced at chat
# time — kept as two constants (not one shared import) because this
# module must stay free of app.models/app.email_sender imports.
PLAN_DEFAULT_CONVERSATION_LIMITS = {"small": 150, "starter": 500, "growth": 1500}


class StoreConfigError(Exception):
    """Raised when a store config file is missing or invalid."""


class FAQEntry(BaseModel):
    question: str
    answer: str


class OverrideEntry(BaseModel):
    """A manual correction for one store, set via scripts/set_override.py —
    e.g. a stale dispatch-time fact drafted from the site that's since
    changed. Rendered into the system prompt as the single highest-priority
    source (see app.agent.system_prompt.build_system_prompt), above the
    knowledge base itself — a scrape/refresh (scripts/create_demo.py,
    app.live_onboarding, scripts/provision_store.py) must never add, remove
    or touch these; only set_override.py does.
    """

    topic: str
    answer: str


class KnowledgeBase(BaseModel):
    shipping: str = ""
    returns: str = ""
    sizing: str = ""
    # The store's own stated claims about itself/its products — pet safety,
    # ingredients/materials, vegan, handmade, made in the UK, sustainability
    # — drawn from the homepage, About/Our Story page and FAQ, not from a
    # policy page. Kept separate from shipping/returns/sizing since these
    # are brand claims, not policy, but answered the same way (only what's
    # actually stated on the site — see app.demo_kb.draft_knowledge_base).
    brand_claims: str = ""
    # A current store-wide multi-buy/bundle/sale deal actually stated on
    # the site (e.g. a "5 for £15" badge, an announcement-bar banner) —
    # kept separate from brand_claims since it's time-limited and must
    # always be phrased as "confirm at checkout", never a guaranteed
    # ongoing price (see app.demo_kb.draft_knowledge_base).
    promotions: str = ""
    faq: list[FAQEntry] = Field(default_factory=list)


class StoreConfig(BaseModel):
    store_id: str
    store_name: str
    shop_domain: str
    public_domain: str
    admin_token_env: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    support_email: str
    # Where a handoff (app.agent.handoff / the "Talk to a person" button)
    # actually gets emailed — defaults to support_email when unset, so
    # this only needs setting when the shop wants handoffs routed
    # somewhere else (e.g. a dedicated support inbox).
    handoff_email: str | None = None
    # Shown verbatim in the handoff confirmation message when set (e.g.
    # "We usually reply within one business day.") — the bot must never
    # promise a response time otherwise; see app.agent.handoff
    # .confirmation_message and the matching system-prompt rule.
    handoff_response_time: str | None = None
    tone: str
    brand_color: str
    greeting: str
    hint_text: str
    # Optional small line shown under the widget's header title (see
    # app/static/widget.js) — e.g. for tillbird-site's own "meta" assistant
    # to disclose "This assistant runs on Tillbird too."
    widget_tagline: str | None = None
    allowed_origins: list[str] = Field(default_factory=list)
    knowledge_base: KnowledgeBase = Field(default_factory=KnowledgeBase)
    # Manual per-store corrections — see OverrideEntry. Kept separate from
    # knowledge_base (rather than just editing that field directly) so a
    # scrape/refresh can safely overwrite knowledge_base wholesale without
    # ever touching a correction Lev made by hand.
    overrides: list[OverrideEntry] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    mode: Literal["live", "demo"]
    # False only for a "meta" assistant like tillbird-site, which answers
    # questions about Tillbird itself rather than being the support widget
    # for an actual shop — it has no real orders or products, so the
    # search_products/lookup_order tools (and the system prompt guidance
    # for them) aren't offered at all. A prompt instruction alone ("never
    # look up orders") isn't reliable enough on its own — see
    # app.agent.tools.build_tool_schemas.
    is_shop: bool = True
    # Only relevant for stores whose Shopify order number format (Settings >
    # Orders in Shopify admin) isn't the plain default "#1234" — e.g. "EH-1234".
    order_number_prefix: str = ""
    order_number_suffix: str = ""
    privacy_policy_url: str | None = None
    # Set by scripts/create_demo.py; used to expire demo links (see
    # StoreConfig.is_demo_expired and app/routes/widget.py). ISO 8601 date.
    demo_created_at: str | None = None
    # Per-store overrides for app/routes/chat.py's global message caps — None
    # means "use the global default". A one-off sales demo is fine with the
    # global defaults; a permanent, publicly-embedded flagship demo (e.g.
    # stores/site-demo.yaml on the marketing site) needs its own budget: a
    # much higher (or effectively disabled) lifetime total, a higher daily
    # cap suited to real traffic, and copy that fits where it's embedded.
    max_session_messages: int | None = None
    demo_total_cap: int | None = None
    demo_daily_cap: int | None = None
    demo_cap_message: str | None = None
    # The fixed demo order (order 1001 / demo@example.com — see
    # app.agent.order_lookup) has a hardcoded line item by default ("Merino
    # Crew Jumper"), left over from the fashion-store fixture it was first
    # built for. A store selling anything else should override it so the
    # demo order doesn't show a product that makes no sense for that shop.
    # Each item: {"title": str, "variant": str | None, "quantity": int}.
    demo_order_line_items: list[dict] | None = None
    # Which pricing plan a live client is on (see site/index.html's pricing
    # section) — informational for now, so it can be recorded per client as
    # they're onboarded; no usage-metering/enforcement code reads this yet.
    # monthly_conversation_limit defaults to the plan's standard allowance
    # if not set explicitly, but can still be overridden per client if ever
    # agreed differently.
    plan: Literal["small", "starter", "growth"] | None = None
    monthly_conversation_limit: int | None = None
    # A live store onboarded via the OAuth install flow (app/routes/oauth.py)
    # gets its knowledge base auto-drafted immediately, but that draft isn't
    # trustworthy yet — "pending_review" blocks /api/chat (see
    # app/routes/chat.py) until scripts/approve_live_store.py sets this to
    # "active". "inactive" is set by the app/uninstalled webhook — the
    # widget stops answering, but the YAML/knowledge base is kept in case
    # the client reinstalls later. None (the default for every store
    # written by hand or by scripts/create_demo.py) behaves exactly like
    # "active" — this only matters for the OAuth-onboarded path.
    live_status: Literal["pending_review", "active", "inactive"] | None = None
    # Billing/account status, set by scripts/set_store_status.py — separate
    # from live_status above (that's about whether the auto-draft has been
    # reviewed; this is about whether the client is still a paying,
    # non-paused account at all). None (every store written before this
    # field existed, e.g. stores/emberhome.yaml) behaves exactly like
    # "active" — deploying this must never silently go dark for an
    # existing live store. Only ever read via is_access_blocked() below.
    status: Literal["trial", "active", "paused", "cancelled"] | None = None
    # ISO date "YYYY-MM-DD" — only meaningful when status is "trial".
    trial_ends_at: str | None = None

    @field_validator("support_email")
    @classmethod
    def _support_email_must_look_valid(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError(f"support_email is not a valid email address: {v!r}")
        return v

    @model_validator(mode="after")
    def _default_conversation_limit_from_plan(self) -> "StoreConfig":
        if self.monthly_conversation_limit is None and self.plan is not None:
            self.monthly_conversation_limit = PLAN_DEFAULT_CONVERSATION_LIMITS[self.plan]
        return self

    @model_validator(mode="after")
    def _live_mode_requires_shopify_credentials(self) -> "StoreConfig":
        if self.mode == "live":
            # Only live mode calls the real Shopify Admin API, so only live
            # mode needs a real *.myshopify.com domain — a demo store scraped
            # from a public custom domain (e.g. emberhome.uk) never calls it.
            if not self.shop_domain.endswith(".myshopify.com"):
                raise ValueError("shop_domain must end with '.myshopify.com' when mode is 'live'")
            if bool(self.client_id_env) != bool(self.client_secret_env):
                raise ValueError("client_id_env and client_secret_env must be set together")
            # No further check here: a live store with NEITHER admin_token_env
            # nor client_id_env/client_secret_env set is assumed to use the
            # OAuth credentials table instead (see app.shopify_client
            # .build_client_for_store and app/routes/oauth.py) — checked via
            # a DB lookup at the point a client is actually built, not here
            # (this validator only sees the YAML, never the database).
        return self

    def uses_client_credentials(self) -> bool:
        return bool(self.client_id_env and self.client_secret_env)

    def uses_oauth_credentials(self) -> bool:
        """True when this store's credentials live in the OAuth table
        rather than being named by a YAML field — see
        app.shopify_client.build_client_for_store. Doesn't check the
        database itself (that lookup happens there); just reflects what
        the YAML does/doesn't declare.
        """
        return self.mode == "live" and not self.admin_token_env and not self.uses_client_credentials()

    def is_demo_expired(self, expiry_days: int) -> bool:
        """True if this is a demo store whose demo_created_at is older than
        expiry_days. Stores with no demo_created_at (e.g. the hand-made
        demo-example fixture) never expire.
        """
        if self.mode != "demo" or not self.demo_created_at:
            return False
        try:
            created = datetime.fromisoformat(self.demo_created_at)
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        return age_days > expiry_days

    def is_access_blocked(self) -> bool:
        """True if the widget must stay silent (no launcher bubble at all
        — see app/routes/widget.py's widget_config) and /api/chat must
        refuse politely (see app/routes/chat.py) for this store.

        "paused"/"cancelled" always block. "trial" blocks once past
        trial_ends_at (a trial with no trial_ends_at set, or an
        unparseable one, is never blocked — fail open rather than
        silently lock someone out of their own trial over a config typo).
        "active" or unset (every store written before this field existed)
        never blocks. Demo stores use their own separate is_demo_expired
        and are never affected by this at all.
        """
        if self.mode != "live":
            return False
        if self.status in ("paused", "cancelled"):
            return True
        if self.status == "trial":
            if not self.trial_ends_at:
                return False
            try:
                ends = date.fromisoformat(self.trial_ends_at)
            except ValueError:
                return False
            return date.today() > ends
        return False

    def get_admin_token(self) -> str:
        """Read the store's static Shopify Admin API token from the env var it names.

        Only for stores using the legacy static-token flow (admin_token_env);
        stores using the client credentials grant should call
        get_shopify_client_credentials() instead — see app.shopify_client.
        """
        if self.mode != "live":
            raise StoreConfigError(f"store {self.store_id!r} is not in live mode; no admin token to read")
        if not self.admin_token_env:
            raise StoreConfigError(
                f"store {self.store_id!r} has no admin_token_env configured "
                "(it uses the client credentials grant instead)"
            )
        token = os.environ.get(self.admin_token_env, "")
        if not token:
            raise StoreConfigError(
                f"env var {self.admin_token_env!r} (admin_token_env for store {self.store_id!r}) is not set"
            )
        return token

    def get_shopify_client_credentials(self) -> tuple[str, str]:
        """Read the store's Shopify client ID + secret from the env vars they name.

        Used for the client credentials grant (Dev Dashboard apps); see
        app.shopify_client.build_client_for_store.
        """
        if self.mode != "live":
            raise StoreConfigError(f"store {self.store_id!r} is not in live mode; no Shopify credentials to read")
        if not self.uses_client_credentials():
            raise StoreConfigError(
                f"store {self.store_id!r} has no client_id_env/client_secret_env configured"
            )
        client_id = os.environ.get(self.client_id_env, "")
        client_secret = os.environ.get(self.client_secret_env, "")
        if not client_id or not client_secret:
            raise StoreConfigError(
                f"env vars {self.client_id_env!r} / {self.client_secret_env!r} "
                f"(client_id_env/client_secret_env for store {self.store_id!r}) are not both set"
            )
        return client_id, client_secret


def load_store_config(store_id: str, stores_dir: Path | str = "stores") -> StoreConfig:
    stores_dir = Path(stores_dir)
    path = stores_dir / f"{store_id}.yaml"
    if not path.is_file():
        raise StoreConfigError(f"no config found for store {store_id!r} (expected {path})")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if raw.get("store_id") != store_id:
        raise StoreConfigError(
            f"store_id in {path} ({raw.get('store_id')!r}) does not match filename ({store_id!r})"
        )

    try:
        return StoreConfig.model_validate(raw)
    except Exception as exc:
        raise StoreConfigError(f"invalid config for store {store_id!r} ({path}): {exc}") from exc


def list_store_ids(stores_dir: Path | str = "stores") -> list[str]:
    stores_dir = Path(stores_dir)
    if not stores_dir.is_dir():
        return []
    return sorted(p.stem for p in stores_dir.glob("*.yaml"))
