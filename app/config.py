import os
from pathlib import Path


class AppConfig:
    """Process-wide configuration, read from environment variables."""

    def __init__(self):
        self.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

        # The audit (scripts/audit_store.py) uses a cheaper model for the
        # high-volume extraction/classification calls, and only reaches for
        # the main anthropic_model above for the single, low-volume final
        # contradiction-confirmation check — see app.audit.run_audit.
        self.audit_fast_model = os.environ.get("AUDIT_FAST_MODEL", "claude-haiku-4-5-20251001")

        self.shopify_api_version = os.environ.get("SHOPIFY_API_VERSION", "")

        # "prefer-ipv4" (default), "ipv4-only", or "system" — see app.net.
        # Guards against a host that resolves a destination to both an
        # IPv4 and IPv6 address but has no actual IPv6 route (seen on a
        # Hetzner VPS), which can make outbound Shopify/Anthropic/scraping
        # requests fail outright instead of falling back to IPv4 on their
        # own.
        self.outbound_ip_family = os.environ.get("OUTBOUND_IP_FAMILY", "prefer-ipv4")

        # OAuth install flow for client stores (app/routes/oauth.py,
        # app/shopify_oauth.py). A custom-distribution app only installs on
        # the ONE store (or Plus org) it was generated for, so there's a
        # SEPARATE app per client, not one shared app — client_id/secret
        # live per-store in app.models.ShopifyAppCredential (see
        # scripts/add_client_app.py), not here. Scopes ARE the same for
        # every client app (set identically in each app's Configuration in
        # the Partner Dashboard), so that one stays a single global value.
        self.shopify_oauth_scopes = os.environ.get("SHOPIFY_OAUTH_SCOPES", "read_orders,read_products")
        # How long an /oauth/install -> /oauth/callback round trip has to
        # complete before its CSRF state nonce (app.models.OAuthState)
        # expires and the install must be restarted.
        self.oauth_state_expiry_minutes = int(os.environ.get("OAUTH_STATE_EXPIRY_MINUTES", "10"))

        # Fernet key (cryptography.fernet.Fernet.generate_key()) encrypting
        # OAuth tokens at rest (app.credential_encryption,
        # app.models.ShopifyOAuthCredential) — the single most sensitive
        # secret in the system once any client is live. Generate once, never
        # commit, never rotate casually (rotating without re-encrypting every
        # existing row first locks out every connected client).
        self.credential_encryption_key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "")

        # Assumed per-store Anthropic cost for the automatic knowledge-base
        # draft run right after a client installs (app.live_onboarding) —
        # same flat-estimate approach as scripts/audit_store.py. The install
        # itself always succeeds even if this cap is hit; only the KB draft
        # is skipped (left for a manual follow-up run).
        self.live_onboarding_max_cost = float(os.environ.get("LIVE_ONBOARDING_MAX_COST", "1.00"))

        database_path = os.environ.get("DATABASE_PATH", "instance/app.db")
        # Flask-SQLAlchemy resolves relative sqlite:/// paths against the
        # app's instance folder, not the CWD, so make this absolute to keep
        # it predictable regardless of where the app is created from.
        abs_db_path = Path(database_path).expanduser().resolve()
        self.database_uri = f"sqlite:///{abs_db_path.as_posix()}"

        self.stores_dir = Path(os.environ.get("STORES_DIR", "stores"))

        # Number of trusted reverse proxies in front of this app (e.g. 1 for
        # a single Caddy instance). 0 (default) means "not behind a proxy" —
        # X-Forwarded-* headers are ignored, since trusting them from an
        # untrusted client would let it spoof request.scheme/host. Only set
        # this when you control the proxy and it always sets these headers.
        self.trusted_proxy_count = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))

        # Demo links (scripts/create_demo.py) stop working after this many
        # days — see StoreConfig.is_demo_expired and app/routes/widget.py.
        self.demo_expiry_days = int(os.environ.get("DEMO_EXPIRY_DAYS", "30"))

        # Audit report links (scripts/audit_store.py) stop working after this
        # many days — see app.models.AuditReport and app/routes/audit.py.
        self.audit_expiry_days = int(os.environ.get("AUDIT_EXPIRY_DAYS", "60"))

        # A conversation (Session + its Messages/ToolCalls/Handoffs) older
        # than this is deleted by scripts/purge_old_conversations.py (run
        # daily from cron — see docs/DEPLOY.md). Privacy basics, not a
        # feature any store config can override.
        self.conversation_retention_days = int(os.environ.get("CONVERSATION_RETENTION_DAYS", "90"))

        # Shared secret internal tooling (scripts/test_demo.py) sends as the
        # X-Tillbird-Internal-Test header on /api/chat to identify itself as
        # our own testing traffic, not a real prospect — see app/routes/chat.py.
        # Confirmed live: repeated manual testing against CharlartsCrafts'
        # demo this way ate its public DEMO_MESSAGE_CAP_DAILY, so a real
        # prospect opening the link saw "usage limit reached" instead of a
        # working demo. Empty by default (feature inert until set) — fails
        # safe: an unset/wrong token is just treated as ordinary public
        # traffic, never silently granted a bypass.
        self.internal_test_token = os.environ.get("INTERNAL_TEST_TOKEN", "")

        # Tillbird's own privacy page, linked from the widget's small
        # "AI assistant · may make mistakes" disclosure (see
        # app/routes/widget.py, app/static/widget.js) — distinct from a
        # store's own privacy_policy_url (StoreConfig), which is THEIR
        # policy, not Tillbird's.
        self.tillbird_privacy_url = os.environ.get("TILLBIRD_PRIVACY_URL", "https://tillbird.co.uk/privacy")

        # SMTP for handoff emails (app/email_sender.py). Empty host means "not
        # configured" — handoffs still work, just log-only (see
        # app/agent/handoff.py and README "Handoff (before SMTP exists)").
        self.smtp_host = os.environ.get("SMTP_HOST", "")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_username = os.environ.get("SMTP_USERNAME", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self.smtp_from = os.environ.get("SMTP_FROM", "")

        # This server's own public URL, e.g. "https://app.tillbird.co.uk" — no
        # trailing slash. Used by scripts/create_demo.py to print a real,
        # shareable demo link instead of localhost. Empty in local dev.
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

        # Where "get a free demo" form submissions (app/routes/leads.py) get
        # emailed. The lead is always stored in the DB regardless of SMTP
        # being configured — see scripts/list_leads.py as the fallback way
        # to see them.
        self.lead_notify_email = os.environ.get("LEAD_NOTIFY_EMAIL", "lev@tillbird.co.uk")

        # Where monthly-usage threshold alerts (app.usage_limits) and
        # trial-ended notices (app.store_status) get emailed — always me,
        # never the shop owner. Empty means "don't send" (both checks are
        # best-effort no-ops without it), same convention as smtp_host.
        self.alert_email = os.environ.get("ALERT_EMAIL", "lev@tillbird.co.uk")

        # Extra CORS origins allowed on top of every store's own
        # allowed_origins (see app/routes/chat.py) — for testing the site
        # locally (e.g. http://localhost:9000) against a real running app
        # server without editing any store's YAML. Comma-separated. Empty by
        # default — only set this in local dev, never in production.
        self.dev_extra_origins = [o.strip() for o in os.environ.get("DEV_EXTRA_ORIGINS", "").split(",") if o.strip()]
