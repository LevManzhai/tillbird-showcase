"""Idempotency helpers for a one-off internal (Lev-only, never the shop
owner) notification email that must never be sent twice for the same
(store, kind, period) — see app.models.SentNotification for the table
itself. Shared by app.usage_limits (monthly usage-threshold alerts, kind
"usage_80"/"usage_100") and scripts.notify_trial_endings (kind
"trial_ended").
"""

from app.extensions import db
from app.models import SentNotification


def already_sent(store_id: str, kind: str, period: str) -> bool:
    return db.session.query(SentNotification).filter_by(store_id=store_id, kind=kind, period=period).first() is not None


def mark_sent(store_id: str, kind: str, period: str) -> None:
    db.session.add(SentNotification(store_id=store_id, kind=kind, period=period))
    db.session.commit()
