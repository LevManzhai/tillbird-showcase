# The customer-facing confirmation is built per-store (see
# confirmation_message below) — the actual email send happens in
# app/routes/chat.py's _send_handoff_email_best_effort, right after this
# tool call returns "ok" (it needs app_config/SMTP settings, which this
# handler doesn't have access to).
MISSING_EMAIL_MESSAGE = (
    "I need an email address to connect you with someone — could you share it? "
    "Or you can use the \"Talk to a person\" button below."
)

HANDOFF_TO_HUMAN_SCHEMA = {
    "name": "handoff_to_human",
    "description": (
        "Connect the customer with a human team member. Use this when the customer is angry, "
        "explicitly asks for a person, the issue involves a refund, a damaged item or a payment, "
        "or you've failed to help them twice in a row."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_email": {"type": "string", "description": "The customer's email address"},
            "summary": {"type": "string", "description": "Short summary of the issue, for the team"},
        },
        "required": ["customer_email", "summary"],
    },
}


def confirmation_message(store_config) -> str:
    """Never promises a specific response TIME unless the store's yaml
    states one (handoff_response_time) — see StoreConfig and the matching
    "don't promise a response time" rule in app.agent.system_prompt.
    """
    base = "Thanks — I've passed this on to our team."
    if store_config.handoff_response_time:
        return f"{base} {store_config.handoff_response_time}"
    return f"{base} They'll get back to you by email."


def handle_handoff_to_human(ctx, tool_input: dict) -> tuple[dict, str]:
    email = (tool_input.get("customer_email") or "").strip()
    summary = (tool_input.get("summary") or "").strip()
    if not email:
        return {"handed_off": False, "message": MISSING_EMAIL_MESSAGE}, "error"

    return {"handed_off": True, "message": confirmation_message(ctx.store_config)}, "ok"
