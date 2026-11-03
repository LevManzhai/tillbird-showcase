from dataclasses import dataclass, field
from difflib import get_close_matches

from app.agent.handoff import HANDOFF_TO_HUMAN_SCHEMA, handle_handoff_to_human
from app.agent.knowledge_base import is_knowledge_base_small
from app.agent.order_lookup import LOOKUP_ORDER_SCHEMA, LookupBudget, handle_lookup_order
from app.catalog import CatalogError, get_product_catalog

SEARCH_PRODUCTS_SCHEMA = {
    "name": "search_products",
    "description": (
        "Search this store's products by name or keyword. Returns up to 10 matches "
        "with price, variants/sizes and stock availability. You can pass several "
        "comma-separated keywords in one call to match any of them — e.g. checking "
        "several ingredient synonyms at once — rather than calling this once per keyword."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Product name or keyword, or several comma-separated keywords to match any of",
            },
        },
        "required": ["query"],
    },
}

GET_POLICY_SCHEMA = {
    "name": "get_policy",
    "description": (
        "Look up this store's policy text on shipping, returns, sizing/garment care (e.g. washing, dry "
        "cleaning), a brand/product claim (e.g. pet safety, ingredients, materials, vegan, handmade, "
        "sustainability), or a FAQ topic. Always use this instead of guessing policy or product details."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "e.g. 'shipping', 'returns', 'how should I wash this', 'is this safe for pets', or the customer's question",
            },
        },
        "required": ["topic"],
    },
}


def build_tool_schemas(store_config) -> list[dict]:
    """Tool schemas offered to the model for this store.

    get_policy is only offered as a fallback for stores whose knowledge base
    is too large to inline in the system prompt (see build_system_prompt) —
    for small knowledge bases the full text is already in context, so the
    tool round-trip (and its fuzzy-match risk) is unnecessary.

    search_products/lookup_order are only offered when store_config.is_shop
    — a "meta" assistant like tillbird-site (answering questions about
    Tillbird itself, not acting as a shop's support widget) has no real
    orders or products, so those tools are withheld entirely rather than
    relying on a prompt instruction not to use them.
    """
    schemas = [HANDOFF_TO_HUMAN_SCHEMA]
    if store_config.is_shop:
        schemas = [SEARCH_PRODUCTS_SCHEMA, LOOKUP_ORDER_SCHEMA] + schemas
    if not is_knowledge_base_small(store_config.knowledge_base):
        schemas.append(GET_POLICY_SCHEMA)
    return schemas

_SHIPPING_KEYWORDS = {"shipping", "delivery", "deliver", "ship", "post", "postage", "carrier", "dispatch"}
_RETURNS_KEYWORDS = {"return", "returns", "refund", "exchange"}
_SIZING_KEYWORDS = {
    "size", "sizing", "fit", "fits", "measurement",
    "wash", "washing", "care", "dry clean", "tumble", "iron", "ironing", "garment care",
}
_BRAND_CLAIMS_KEYWORDS = {
    "pet", "pets", "dog", "dogs", "cat", "cats", "animal", "animals",
    "vegan", "cruelty", "cruelty-free", "allergy", "allergen", "ingredient", "ingredients",
    "material", "materials", "fabric", "scent", "fragrance",
    "handmade", "hand-made", "hand poured", "hand-poured",
    "sustainable", "sustainability", "eco", "eco-friendly", "recycled", "recyclable",
    "organic", "natural", "made in the uk", "made in uk", "british made", "local",
}


@dataclass
class ToolContext:
    store_config: "app.store_config.StoreConfig"
    app_config: "app.config.AppConfig"
    lookup_budget: LookupBudget = field(default_factory=LookupBudget)


def _match_policy(kb, topic: str) -> str | None:
    # Check FAQ first: a close match to a specific customer question is more
    # useful than a generic category blurb (e.g. "ship to Ireland?" should hit
    # the Ireland FAQ entry, not the generic shipping keyword bucket below).
    if kb.faq:
        questions = [entry.question for entry in kb.faq]
        best = get_close_matches(topic, questions, n=1, cutoff=0.5)
        if best:
            for entry in kb.faq:
                if entry.question == best[0]:
                    return entry.answer

    topic_lower = topic.lower()
    if any(kw in topic_lower for kw in _SHIPPING_KEYWORDS) and kb.shipping:
        return kb.shipping
    if any(kw in topic_lower for kw in _RETURNS_KEYWORDS) and kb.returns:
        return kb.returns
    if any(kw in topic_lower for kw in _SIZING_KEYWORDS) and kb.sizing:
        return kb.sizing
    if any(kw in topic_lower for kw in _BRAND_CLAIMS_KEYWORDS) and kb.brand_claims:
        return kb.brand_claims
    return None


def handle_get_policy(ctx: ToolContext, tool_input: dict) -> tuple[dict, str]:
    topic = (tool_input.get("topic") or "").strip()
    if not topic:
        return {"found": False}, "error"
    text = _match_policy(ctx.store_config.knowledge_base, topic)
    if text is None:
        return {"found": False}, "not_found"
    return {"found": True, "text": text}, "ok"


def handle_search_products(ctx: ToolContext, tool_input: dict) -> tuple[dict, str]:
    query = (tool_input.get("query") or "").strip()
    if not query:
        return {"products": []}, "error"
    try:
        catalog = get_product_catalog(ctx.store_config, ctx.app_config)
        results = catalog.search(query, limit=10)
    except CatalogError as exc:
        return {"error": str(exc)}, "error"
    if ctx.store_config.mode == "demo" and not ctx.store_config.demo_created_at:
        # No demo_created_at means a permanent, hand-authored fixture (e.g.
        # site-demo, demo-example) rather than a scraped sales demo on the
        # scripts/refresh_demo_catalogs.py lifecycle — see that script's
        # matching skip logic. Its data doesn't decay the way a scrape does,
        # so it's never flagged stale regardless of file age.
        stale = False
    else:
        stale = catalog.is_stale()
    return {
        "products": [r.to_dict() for r in results],
        "stock_data_may_be_stale": stale,
    }, "ok"


TOOL_HANDLERS = {
    "search_products": handle_search_products,
    "get_policy": handle_get_policy,
    "lookup_order": handle_lookup_order,
    "handoff_to_human": handle_handoff_to_human,
}


def call_tool(name: str, tool_input: dict, ctx: ToolContext) -> tuple[dict, str]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name!r}"}, "error"
    try:
        return handler(ctx, tool_input)
    except Exception as exc:
        return {"error": str(exc)}, "error"
