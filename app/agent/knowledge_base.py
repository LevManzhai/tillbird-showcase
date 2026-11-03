KB_SMALL_TOKEN_THRESHOLD = 8000


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token for English text).

    Good enough for the inline-vs-tool threshold decision below; not used for
    billing or anything that needs real tokenization.
    """
    return max(1, len(text) // 4)


def render_knowledge_base(kb) -> str:
    parts = []
    if kb.shipping:
        parts.append(
            "Shipping (QUOTED SOURCE — state only what these exact words say; never conclude a delivery "
            f"time, cost or carrier it doesn't literally state):\n{kb.shipping}"
        )
    if kb.returns:
        parts.append(
            "Returns (QUOTED SOURCE — state only what these exact words say. A broad opening line like "
            "'money back guarantee on all products' does NOT override a condition or exclusion stated "
            "later in this same text — read the whole thing before answering. If the customer's reason "
            "is a preference, not a defect/damage/wrong item, and this text either limits approval to "
            "those cases or lists excluded categories the product might plausibly fall under, do not say "
            f"'yes, you can return it' — say you can't confirm that specific case and offer a handoff):\n{kb.returns}"
        )
    if kb.sizing:
        parts.append(f"Sizing & care (fit guidance and garment care/washing instructions):\n{kb.sizing}")
    if kb.brand_claims:
        parts.append(f"Brand & product claims (the store's own stated claims, not policy):\n{kb.brand_claims}")
    if kb.promotions:
        parts.append(f"Current promotions (confirm the current terms at checkout, don't promise as fixed):\n{kb.promotions}")
    if kb.faq:
        faq_lines = "\n".join(f"Q: {entry.question}\nA: {entry.answer}" for entry in kb.faq)
        parts.append(f"FAQ:\n{faq_lines}")
    return "\n\n".join(parts)


def is_knowledge_base_small(kb) -> bool:
    return _estimate_tokens(render_knowledge_base(kb)) < KB_SMALL_TOKEN_THRESHOLD
