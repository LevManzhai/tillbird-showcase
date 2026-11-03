"""Monthly conversation limits per plan — cost control for live clients
(see site/index.html's pricing section). One conversation = one widget
session id (app.models.Session.id), counted for the calendar month its
session row was created in.

Config, not hard-coded logic: PLAN_LIMITS is the standard allowance per
plan (used by app.store_config.StoreConfig to default
monthly_conversation_limit when a plan is set but no explicit limit was
given), overridable per store via monthly_conversation_limit: in its yaml.
A store with no limit configured at all (no plan, no explicit limit —
true for every demo and any live store not yet assigned a plan) is never
metered by anything in this module.
"""

import logging
from datetime import datetime

from app.email_sender import send_alert_email
from app.extensions import db
from app.models import Session
from app.notifications import already_sent, mark_sent

logger = logging.getLogger("app.usage_limits")

# Keep in sync with app.store_config.PLAN_DEFAULT_CONVERSATION_LIMITS —
# that dict only sets the DEFAULT monthly_conversation_limit written into
# a store's yaml when it's first given a plan; this is the live source of
# truth actually enforced here. See StoreConfig for why they're not the
# same object (store_config.py must not import this module just to read a
# constant — this module imports models/email, store_config.py doesn't).
PLAN_LIMITS = {"small": 150, "starter": 500, "growth": 1500}

# One alert per store per threshold per calendar month (see
# _alert_already_sent) — not "80% then 100% then nothing", a fresh pair
# fires again next month if usage is still climbing.
ALERT_THRESHOLDS = (0.8, 1.0)

# Above this, the assistant stops answering for the rest of the calendar
# month rather than risk unbounded Anthropic spend on top of an already
# very-over-plan client — see HARD_CAP_MESSAGE. Between 100% and this, a
# paying client's customers are NEVER cut off silently — only the alert
# emails fire, answering continues normally.
HARD_CAP_RATIO = 1.5

HARD_CAP_MESSAGE = "Our assistant is unavailable right now, please email us at {support_email}."


def _month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def conversations_this_month(store_id: str, now: datetime) -> int:
    start, end = _month_bounds(now)
    return (
        db.session.query(Session)
        .filter(Session.store_id == store_id, Session.created_at >= start, Session.created_at < end)
        .count()
    )


def monthly_limit_for(store_config) -> int | None:
    """None for anything that isn't a paying live client — a demo has its
    own separate demo_total_cap/demo_daily_cap mechanism and must never be
    metered here too, even in the unlikely case one somehow had a
    monthly_conversation_limit set by mistake.
    """
    if store_config.mode != "live":
        return None
    return store_config.monthly_conversation_limit or None


def is_over_hard_cap(count: int, limit: int) -> bool:
    return count > limit * HARD_CAP_RATIO


def check_and_alert_usage(app_config, store_config, count: int, limit: int, now: datetime) -> None:
    """Best-effort: emails app_config.alert_email (me, never the shop
    owner) at most once per (store, threshold, calendar month) the first
    time `count` crosses 80%/100% of `limit`. Never raises — a failed or
    unconfigured send just means no alert this check; it's left UNMARKED
    on failure so the next request's check retries it rather than losing
    the notification for the rest of the month.
    """
    if not app_config.alert_email:
        return
    period = _month_key(now)
    for threshold in ALERT_THRESHOLDS:
        if count < limit * threshold:
            continue
        kind = f"usage_{int(threshold * 100)}"
        if already_sent(store_config.store_id, kind, period):
            continue
        try:
            send_alert_email(
                app_config,
                to_address=app_config.alert_email,
                subject=f"{store_config.store_name}: {int(threshold * 100)}% of its monthly conversation limit",
                body=(
                    f"{store_config.store_name} ({store_config.store_id}) has used {count} of its "
                    f"{limit} conversations allotted for {period} — {int(threshold * 100)}% threshold.\n\n"
                    "No action needed if this is expected growth — this is informational only; their "
                    "customers keep being answered normally up to 150% of the plan."
                ),
            )
        except Exception:
            logger.warning("failed to send usage alert store_id=%s kind=%s", store_config.store_id, kind, exc_info=True)
            continue
        mark_sent(store_config.store_id, kind, period)
