from dataclasses import dataclass

DEMO_ORDER_NUMBER = "1001"
DEMO_ORDER_EMAIL = "demo@example.com"

# Identical wording for "no such order" and "order exists but email doesn't
# match" — the tool must never reveal which case occurred, otherwise a caller
# could enumerate valid order numbers by watching how the reply differs.
GENERIC_NOT_FOUND_MESSAGE = (
    "I couldn't find a matching order for that order number and email. "
    "Please double-check them, or I can connect you with a person."
)
LIMIT_REACHED_MESSAGE = (
    "You've reached the maximum number of order lookups for this conversation. "
    "I can connect you with a person instead."
)
MISSING_FIELDS_MESSAGE = "Please provide both the order number and the email used to place the order."
DEMO_EXPLANATION_MESSAGE = (
    "This is a demo store, so order lookup only works with the example order "
    "{order_number} and email {email} — try that to see how it works!"
)
DEMO_ORDER_NOTICE = "This is a demo order to show how I'd answer your customers:"

LOOKUP_ORDER_SCHEMA = {
    "name": "lookup_order",
    "description": (
        "Look up a customer's order by order number and the email address used to place it. "
        "Only returns order details if the email matches the order; otherwise reports no matching "
        "order was found. Ask the customer for both values before calling this."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "description": "The order number, e.g. '1001' or '#1001'"},
            "email": {"type": "string", "description": "The email address used to place the order"},
        },
        "required": ["order_number", "email"],
    },
}


@dataclass
class LookupBudget:
    """Remaining order-lookup attempts for the current session and IP.

    Seeded from persisted tool_calls counts before a turn starts (see
    app/routes/chat.py) and decremented in-memory as lookup_order is called,
    so a single turn that invokes the tool several times (the model can call
    a tool more than once before anything is committed to SQLite) still can't
    exceed the cap.
    """

    session_remaining: int = 5
    ip_remaining: int = 10


def normalise_order_number(raw, prefix: str = "", suffix: str = "") -> str:
    """Normalise customer-typed order numbers to the store's canonical format.

    Strips whitespace and a leading '#' (Shopify's default order-name marker)
    regardless of config, then strips the store's configured prefix/suffix if
    the customer included it (case-insensitive) to find the bare number, and
    re-attaches prefix/suffix in their configured casing — so "1001",
    "EH-1001" and "#eh-1001" all normalise the same way for a store
    configured with order_number_prefix "EH-".
    """
    if not isinstance(raw, str):
        return ""
    stripped = raw.strip()
    if stripped.startswith("#"):
        stripped = stripped[1:].strip()

    bare = stripped
    if prefix and bare.lower().startswith(prefix.lower()):
        bare = bare[len(prefix):]
    if suffix and bare.lower().endswith(suffix.lower()):
        bare = bare[: len(bare) - len(suffix)]
    bare = bare.strip()

    if not bare:
        return ""
    return f"{prefix}{bare}{suffix}"


DEFAULT_DEMO_LINE_ITEMS = [{"title": "Merino Crew Jumper", "variant": "M", "quantity": 1}]


def _demo_order(order_number: str, email: str, line_items: list[dict] | None = None) -> dict | None:
    if order_number != DEMO_ORDER_NUMBER or email != DEMO_ORDER_EMAIL:
        return None
    return {
        "order_number": "1001",
        "status": "open",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "order_date": "2026-09-20",
        "shipments": [
            {
                "carrier": "Royal Mail",
                "tracking_number": "RM123456789GB",
                # No tracking_url: a real carrier link with a fake number is
                # misleading (it looks clickable/real but goes nowhere useful).
                "tracking_url": None,
            }
        ],
        # store_config.demo_order_line_items overrides this per store (see
        # StoreConfig) — the fixed fashion-store fixture makes no sense for
        # e.g. a home & gifts shop's demo.
        "line_items": line_items or DEFAULT_DEMO_LINE_ITEMS,
    }


def _live_order(ctx, order_number: str, email: str) -> dict | None:
    from app.shopify_client import build_client_for_store

    client = build_client_for_store(ctx.store_config, ctx.app_config)
    return client.get_order(order_number, email)


def handle_lookup_order(ctx, tool_input: dict) -> tuple[dict, str]:
    budget = ctx.lookup_budget
    if budget.session_remaining <= 0:
        return {"found": False, "message": LIMIT_REACHED_MESSAGE}, "blocked_session_limit"
    if budget.ip_remaining <= 0:
        return {"found": False, "message": LIMIT_REACHED_MESSAGE}, "blocked_ip_limit"

    budget.session_remaining -= 1
    budget.ip_remaining -= 1

    order_number = normalise_order_number(
        tool_input.get("order_number"),
        prefix=ctx.store_config.order_number_prefix,
        suffix=ctx.store_config.order_number_suffix,
    )
    email = (tool_input.get("email") or "").strip().lower()
    if not order_number or not email:
        return {"found": False, "message": MISSING_FIELDS_MESSAGE}, "error"

    try:
        if ctx.store_config.mode == "demo":
            order = _demo_order(order_number, email, line_items=ctx.store_config.demo_order_line_items)
        else:
            order = _live_order(ctx, order_number, email)
    except Exception:
        return {"found": False, "message": GENERIC_NOT_FOUND_MESSAGE}, "error"

    if order is None:
        if ctx.store_config.mode == "demo":
            message = DEMO_EXPLANATION_MESSAGE.format(order_number=DEMO_ORDER_NUMBER, email=DEMO_ORDER_EMAIL)
            return {"found": False, "message": message}, "demo_mismatch"
        return {"found": False, "message": GENERIC_NOT_FOUND_MESSAGE}, "not_found"

    if ctx.store_config.mode == "demo":
        return {"found": True, "order": order, "demo_notice": DEMO_ORDER_NOTICE}, "ok"
    return {"found": True, "order": order}, "ok"
