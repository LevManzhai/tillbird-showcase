_SENSITIVE_KEYS = {"email", "customer_email"}


def mask_sensitive_args(args: dict) -> dict:
    masked = dict(args)
    for key in list(masked):
        if key.lower() in _SENSITIVE_KEYS and isinstance(masked[key], str):
            masked[key] = _mask_email(masked[key])
    return masked


def _mask_email(email: str) -> str:
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    if len(local) <= 2:
        masked_local = local[:1] + "*" * max(len(local) - 1, 1)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"
