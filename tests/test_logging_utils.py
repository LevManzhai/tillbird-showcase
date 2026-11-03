from app.agent.logging_utils import mask_sensitive_args


def test_mask_sensitive_args_masks_lookup_order_email():
    """C8 privacy basics: a customer's email for order lookup must never
    be stored in plain text in a persisted ToolCall row."""
    masked = mask_sensitive_args({"order_number": "1001", "email": "jane@example.com"})
    assert masked["email"] != "jane@example.com"
    assert "@example.com" in masked["email"]
    assert masked["order_number"] == "1001"


def test_mask_sensitive_args_masks_handoff_customer_email():
    masked = mask_sensitive_args({"customer_email": "jane@example.com", "summary": "damaged item"})
    assert masked["customer_email"] != "jane@example.com"
    assert masked["summary"] == "damaged item"


def test_mask_sensitive_args_leaves_non_sensitive_fields_untouched():
    masked = mask_sensitive_args({"query": "jumper"})
    assert masked == {"query": "jumper"}


def test_mask_sensitive_args_does_not_mutate_the_original_dict():
    original = {"email": "jane@example.com"}
    mask_sensitive_args(original)
    assert original["email"] == "jane@example.com"


def test_mask_sensitive_args_short_local_part():
    masked = mask_sensitive_args({"email": "jo@example.com"})
    assert masked["email"] != "jo@example.com"
    assert masked["email"].endswith("@example.com")
