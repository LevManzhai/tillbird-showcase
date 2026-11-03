import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_

from app.agent.handoff import MISSING_EMAIL_MESSAGE as HANDOFF_MISSING_EMAIL_MESSAGE
from app.agent.handoff import confirmation_message as handoff_confirmation_message
from app.agent.loop import run_agent_turn
from app.agent.logging_utils import mask_sensitive_args
from app.agent.order_lookup import LookupBudget
from app.email_sender import is_smtp_configured, send_handoff_email
from app.extensions import db
from app.models import Handoff, Message, Session, ToolCall
from app.store_config import StoreConfigError, list_store_ids, load_store_config
from app.usage_limits import check_and_alert_usage, conversations_this_month, is_over_hard_cap, monthly_limit_for

_handoff_logger = logging.getLogger("app.agent.handoff")
_usage_logger = logging.getLogger("app.usage_limits")


def _send_handoff_email_best_effort(app_config, store_config, customer_email, summary, session_id) -> bool:
    """Attempts real SMTP send if configured; always returns whether it
    succeeded, and NEVER raises — a failed/unconfigured send must not break
    the customer-facing response (see app/agent/handoff.py).
    """
    if not is_smtp_configured(app_config):
        _handoff_logger.warning(
            "SMTP not configured for store %r — handoff logged only, no email sent.", store_config.store_id
        )
        return False

    transcript_rows = (
        db.session.query(Message).filter(Message.session_id == session_id).order_by(Message.created_at).all()
    )
    transcript = [{"role": m.role, "content": m.content} for m in transcript_rows]

    try:
        send_handoff_email(
            app_config,
            store_name=store_config.store_name,
            support_email=store_config.handoff_email or store_config.support_email,
            customer_email=customer_email,
            summary=summary,
            transcript=transcript,
        )
        return True
    except Exception:
        _handoff_logger.warning("Failed to send handoff email for store %r", store_config.store_id, exc_info=True)
        return False

chat_bp = Blueprint("chat", __name__)

MAX_MESSAGE_LENGTH = 1000
MAX_SESSION_MESSAGES = 30
RATE_LIMIT_WINDOW_MINUTES = 10
RATE_LIMIT_MAX_MESSAGES = 20
RATE_LIMIT_MESSAGE = "You're sending messages a little quickly — please wait a moment and try again."
HISTORY_LIMIT = 20
MAX_LOOKUP_ATTEMPTS_PER_SESSION = 5
MAX_LOOKUP_ATTEMPTS_PER_IP_PER_HOUR = 10

# Cost control for shared demo links (see SPEC.md milestone 6): a demo store
# has no real customer, so these cap total Anthropic spend per demo rather
# than per-session/per-IP like the limits above. Raised 20->40 daily
# (2026-10-24): combined with the internal-test exemption below, 20 had
# already been exhausted by our own manual testing before some prospects
# even got to open the link.
DEMO_MESSAGE_CAP_TOTAL = 60
DEMO_MESSAGE_CAP_DAILY = 40
DEMO_CAP_REACHED_MESSAGE = (
    "This demo has reached its usage limit for now — please check back later, "
    "or get in touch if you'd like to see more."
)

# scripts/test_demo.py identifies itself with this header + a shared secret
# (app_config.internal_test_token, from INTERNAL_TEST_TOKEN in .env) so our
# own testing traffic is exempt from every count-based gate below (the
# per-session/per-IP rate-limit windows, the per-session message cap, and
# the demo total/daily cap) — confirmed live, repeated manual testing
# against CharlartsCrafts' demo via this same /api/chat route ate its
# public daily cap, so a real prospect opening the link saw "usage limit
# reached" instead of a working demo. Messages are still generated and
# logged normally (tagged with INTERNAL_TEST_SENTINEL_IP in place of a real
# IP) — only exempt from counting toward these caps, not from being stored.
# Empty/unset token (the default) means this never activates for anyone —
# fails safe, never a silent public bypass.
INTERNAL_TEST_HEADER = "X-Tillbird-Internal-Test"
INTERNAL_TEST_SENTINEL_IP = "internal-test"


def _is_internal_test_request(app_config) -> bool:
    token = app_config.internal_test_token
    if not token:
        return False
    provided = request.headers.get(INTERNAL_TEST_HEADER, "")
    return hmac.compare_digest(provided, token)


def _own_origin() -> str:
    """This server's own origin, e.g. "http://localhost:8000".

    A page we serve ourselves (/demo/<store_id>) is always a legitimate
    caller of /api/chat and /api/handoff regardless of a store's configured
    allowed_origins — those exist to restrict *third-party* embeds, not our
    own demo/testing page. Browsers send an Origin header even for
    same-origin fetch() POSTs, so without this exemption the demo page gets
    rejected by the same check meant for other sites.
    """
    return f"{request.scheme}://{request.host}"


def _origin_allowed_for_store(origin: str, store_config, app_config) -> bool:
    return (
        origin == _own_origin()
        or origin in store_config.allowed_origins
        or origin in app_config.dev_extra_origins
    )


def _all_allowed_origins(app_config) -> set[str]:
    """Union of allowed_origins across all stores plus our own origin, for CORS preflight.

    store_id (needed to check a single store's allowlist) only arrives in the
    POST body, not on the OPTIONS preflight, so preflight is checked against
    every known store; the actual POST/handoff handlers re-check against the
    specific store below.
    """
    origins: set[str] = {_own_origin()}
    origins.update(app_config.dev_extra_origins)
    for store_id in list_store_ids(app_config.stores_dir):
        try:
            cfg = load_store_config(store_id, stores_dir=app_config.stores_dir)
        except StoreConfigError:
            continue
        origins.update(cfg.allowed_origins)
    return origins


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _respond(payload: dict, status: int, origin: str | None):
    response = jsonify(payload)
    response.status_code = status
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


def _preflight_response():
    app_config = current_app.extensions["app_config"]
    origin = request.headers.get("Origin")
    response = current_app.make_default_options_response()
    if origin and origin in _all_allowed_origins(app_config):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


def _resolve_store(store_id, app_config, preflight_origin):
    """Returns (store_config, error_response). error_response is None on success."""
    if not isinstance(store_id, str) or not store_id:
        return None, _respond({"error": "store_id is required"}, 400, preflight_origin)
    try:
        store_config = load_store_config(store_id, stores_dir=app_config.stores_dir)
    except StoreConfigError:
        return None, _respond({"error": "unknown store"}, 404, preflight_origin)
    return store_config, None


def _check_origin(origin, store_config, app_config):
    """Returns (store_origin_or_None, error_response). error_response is None on success."""
    store_origin = origin if (origin and _origin_allowed_for_store(origin, store_config, app_config)) else None
    if origin and not store_origin:
        return None, _respond({"error": "origin not allowed"}, 403, None)
    return store_origin, None


EXPIRED_DEMO_MESSAGE = "This demo has expired and is no longer active."


def _check_demo_not_expired(store_config, app_config, store_origin):
    """Defense in depth: the /demo/<id> page already refuses to load an
    expired demo's widget, but a cached page or a direct API call shouldn't
    still work either. Returns an error_response, or None if fine."""
    if store_config.is_demo_expired(app_config.demo_expiry_days):
        return _respond({"reply": EXPIRED_DEMO_MESSAGE, "handoff": False}, 200, store_origin)
    return None


LIVE_STORE_PENDING_REVIEW_MESSAGE = "This assistant isn't live yet — check back soon."
LIVE_STORE_INACTIVE_MESSAGE = "This assistant is currently unavailable."


def _check_live_store_active(store_config, store_origin):
    """A live store onboarded via the OAuth install flow (app/routes/oauth.py)
    starts out "pending_review" — its auto-drafted knowledge base hasn't
    been reviewed yet (see scripts/approve_live_store.py) — and is marked
    "inactive" by the app/uninstalled webhook. Neither answers real
    questions. Returns an error_response, or None if fine.
    """
    if store_config.live_status == "pending_review":
        return _respond({"reply": LIVE_STORE_PENDING_REVIEW_MESSAGE, "handoff": False}, 200, store_origin)
    if store_config.live_status == "inactive":
        return _respond({"reply": LIVE_STORE_INACTIVE_MESSAGE, "handoff": False}, 200, store_origin)
    return None


ACCESS_BLOCKED_MESSAGE = "This assistant is currently unavailable."


def _check_access_not_blocked(store_config, store_origin):
    """A trial past its end date, or a paused/cancelled live store (see
    StoreConfig.is_access_blocked) — the widget already stays silent (no
    launcher bubble, see app/routes/widget.py), but a cached page or a
    direct API call must still refuse politely rather than answer. Returns
    an error_response, or None if fine.
    """
    if store_config.is_access_blocked():
        return _respond({"reply": ACCESS_BLOCKED_MESSAGE, "handoff": False}, 200, store_origin)
    return None


def _check_monthly_usage(store_config, store_origin, now):
    """Returns (conversations_this_month_or_None, error_response). The
    count is returned even when there's no error, so the caller can reuse
    it for check_and_alert_usage without a second query. None for both
    means this store has no monthly_conversation_limit configured at all
    (every demo, and any live store not yet assigned a plan) — not
    metered here at all.

    Below 150% of the limit, a paying client's customers are NEVER cut
    off silently, no matter how far over 100% they are — only the
    80%/100% alert emails (see check_and_alert_usage, called separately
    by the caller) react to that. Only crossing the hard cap itself
    produces an error_response here.
    """
    limit = monthly_limit_for(store_config)
    if not limit:
        return None, None
    count = conversations_this_month(store_config.store_id, now)
    if is_over_hard_cap(count, limit):
        _usage_logger.warning("monthly_usage_hard_cap store_id=%s count=%s limit=%s", store_config.store_id, count, limit)
        message = f"Our assistant is unavailable right now, please email us at {store_config.support_email}."
        return count, _respond({"reply": message, "handoff": False}, 200, store_origin)
    return count, None


def _get_or_create_session(session_id, store_id, store_origin):
    """Returns (session, error_response). error_response is None on success."""
    if not isinstance(session_id, str) or not session_id:
        return None, _respond({"error": "session_id is required"}, 400, store_origin)
    session = db.session.get(Session, session_id)
    if session is None:
        session = Session(id=session_id, store_id=store_id)
        db.session.add(session)
        db.session.flush()
        return session, None
    if session.store_id != store_id:
        return None, _respond({"error": "session_id already in use"}, 400, store_origin)
    return session, None


@chat_bp.route("/api/chat", methods=["OPTIONS"])
def chat_preflight():
    return _preflight_response()


@chat_bp.route("/api/handoff", methods=["OPTIONS"])
def handoff_preflight():
    return _preflight_response()


@chat_bp.route("/api/chat", methods=["POST"])
def chat():
    app_config = current_app.extensions["app_config"]
    origin = request.headers.get("Origin")
    globally_allowed = origin in _all_allowed_origins(app_config) if origin else False
    preflight_origin = origin if globally_allowed else None

    body = request.get_json(silent=True) or {}
    store_id = body.get("store_id")
    session_id = body.get("session_id")
    message = body.get("message")

    store_config, err = _resolve_store(store_id, app_config, preflight_origin)
    if err:
        return err

    store_origin, err = _check_origin(origin, store_config, app_config)
    if err:
        return err

    expired_response = _check_demo_not_expired(store_config, app_config, store_origin)
    if expired_response:
        return expired_response

    live_gate_response = _check_live_store_active(store_config, store_origin)
    if live_gate_response:
        return live_gate_response

    access_blocked_response = _check_access_not_blocked(store_config, store_origin)
    if access_blocked_response:
        return access_blocked_response

    now = datetime.now(timezone.utc)
    conversation_count, hard_cap_response = _check_monthly_usage(store_config, store_origin, now)
    if hard_cap_response:
        return hard_cap_response
    if conversation_count is not None:
        try:
            check_and_alert_usage(app_config, store_config, conversation_count, monthly_limit_for(store_config), now)
        except Exception:
            # Never let an alert-email hiccup break the actual chat reply.
            _usage_logger.warning("check_and_alert_usage failed store_id=%s", store_config.store_id, exc_info=True)

    if not isinstance(message, str) or not message.strip():
        return _respond({"error": "message is required"}, 400, store_origin)
    if len(message) > MAX_MESSAGE_LENGTH:
        return _respond({"error": f"message must be at most {MAX_MESSAGE_LENGTH} characters"}, 400, store_origin)

    session, err = _get_or_create_session(session_id, store_id, store_origin)
    if err:
        return err

    is_internal_test = _is_internal_test_request(app_config)
    window_start = now - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)

    session_recent = (
        db.session.query(Message)
        .filter(Message.session_id == session_id, Message.role == "user", Message.created_at >= window_start)
        .count()
    )
    if not is_internal_test and session_recent >= RATE_LIMIT_MAX_MESSAGES:
        return _respond({"reply": RATE_LIMIT_MESSAGE, "handoff": False}, 200, store_origin)

    client_ip = INTERNAL_TEST_SENTINEL_IP if is_internal_test else _client_ip()
    ip_recent = (
        db.session.query(Message)
        .filter(Message.ip_address == client_ip, Message.role == "user", Message.created_at >= window_start)
        .count()
    )
    if not is_internal_test and ip_recent >= RATE_LIMIT_MAX_MESSAGES:
        return _respond({"reply": RATE_LIMIT_MESSAGE, "handoff": False}, 200, store_origin)

    total_user_messages = (
        db.session.query(Message).filter(Message.session_id == session_id, Message.role == "user").count()
    )
    session_cap = store_config.max_session_messages or MAX_SESSION_MESSAGES
    if not is_internal_test and total_user_messages >= session_cap:
        reply = (
            f"This conversation has reached its limit. Please email {store_config.support_email} "
            "and we'll help you directly."
        )
        db.session.add(Message(session_id=session_id, role="assistant", content=reply))
        db.session.commit()
        return _respond({"reply": reply, "handoff": True}, 200, store_origin)

    if store_config.mode == "demo" and not is_internal_test:
        total_cap = store_config.demo_total_cap or DEMO_MESSAGE_CAP_TOTAL
        daily_cap = store_config.demo_daily_cap or DEMO_MESSAGE_CAP_DAILY
        cap_message = store_config.demo_cap_message or DEMO_CAP_REACHED_MESSAGE
        # Excludes our own internal-test traffic (tagged with
        # INTERNAL_TEST_SENTINEL_IP) from ever counting toward a real
        # prospect's cap — without this, a past internal-test message would
        # still count here even though ITS OWN request was exempted above.
        # NULL-safe (or_ with .is_(None)): a bare `!=` against NULL is NULL
        # in SQL, not true, so it would have silently excluded every row
        # with no ip_address at all — confirmed while writing this fix's
        # own tests, a plain `Message.ip_address != ...` dropped every
        # fixture-seeded row that hadn't set ip_address, undercounting.
        not_internal_test = or_(Message.ip_address.is_(None), Message.ip_address != INTERNAL_TEST_SENTINEL_IP)
        demo_total = (
            db.session.query(Message)
            .join(Session, Message.session_id == Session.id)
            .filter(Session.store_id == store_id, Message.role == "user", not_internal_test)
            .count()
        )
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        demo_today = (
            db.session.query(Message)
            .join(Session, Message.session_id == Session.id)
            .filter(
                Session.store_id == store_id,
                Message.role == "user",
                Message.created_at >= day_start,
                not_internal_test,
            )
            .count()
        )
        if demo_total >= total_cap or demo_today >= daily_cap:
            db.session.add(Message(session_id=session_id, role="assistant", content=cap_message))
            db.session.commit()
            return _respond({"reply": cap_message, "handoff": False}, 200, store_origin)

    user_msg = Message(session_id=session_id, role="user", content=message, ip_address=client_ip)
    db.session.add(user_msg)
    db.session.flush()

    history_rows = (
        db.session.query(Message)
        .filter(Message.session_id == session_id, Message.id != user_msg.id)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    history_rows.reverse()
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    lookup_window_start = now - timedelta(hours=1)
    session_lookup_count = (
        db.session.query(ToolCall)
        .filter(ToolCall.session_id == session_id, ToolCall.name == "lookup_order")
        .count()
    )
    ip_lookup_count = (
        db.session.query(ToolCall)
        .filter(
            ToolCall.ip_address == client_ip,
            ToolCall.name == "lookup_order",
            ToolCall.created_at >= lookup_window_start,
        )
        .count()
    )
    lookup_budget = LookupBudget(
        session_remaining=max(0, MAX_LOOKUP_ATTEMPTS_PER_SESSION - session_lookup_count),
        ip_remaining=max(0, MAX_LOOKUP_ATTEMPTS_PER_IP_PER_HOUR - ip_lookup_count),
    )

    result = run_agent_turn(app_config, store_config, history, message, lookup_budget=lookup_budget)

    db.session.add(
        Message(
            session_id=session_id,
            role="assistant",
            content=result.reply,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_creation_input_tokens=result.usage.cache_creation_input_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
        )
    )
    for call in result.tool_calls:
        db.session.add(
            ToolCall(
                session_id=session_id,
                name=call.name,
                args=json.dumps(mask_sensitive_args(call.args)),
                status=call.status,
                ip_address=client_ip,
            )
        )
        if call.name == "handoff_to_human" and call.status == "ok":
            customer_email = call.args.get("customer_email", "")
            summary = call.args.get("summary", "")
            email_sent = _send_handoff_email_best_effort(app_config, store_config, customer_email, summary, session_id)
            db.session.add(
                Handoff(
                    session_id=session_id,
                    customer_email=customer_email,
                    summary=summary,
                    email_sent=email_sent,
                )
            )
    session.last_active_at = now
    db.session.commit()

    return _respond({"reply": result.reply, "handoff": result.handoff}, 200, store_origin)


@chat_bp.route("/api/handoff", methods=["POST"])
def handoff():
    """Deterministic handoff path for the widget's "Talk to a person" button.

    Distinct from the agent-invoked handoff_to_human tool (used when the
    model itself decides mid-conversation to hand off): here the customer
    has already explicitly asked via a dedicated UI control, so there's no
    need to route it through the model's judgment — this guarantees the
    button always works, with no dependency on tool-call reliability.
    """
    app_config = current_app.extensions["app_config"]
    origin = request.headers.get("Origin")
    globally_allowed = origin in _all_allowed_origins(app_config) if origin else False
    preflight_origin = origin if globally_allowed else None

    body = request.get_json(silent=True) or {}
    store_id = body.get("store_id")
    session_id = body.get("session_id")
    customer_email = (body.get("customer_email") or "").strip()
    summary = (body.get("summary") or "").strip()

    store_config, err = _resolve_store(store_id, app_config, preflight_origin)
    if err:
        return err

    store_origin, err = _check_origin(origin, store_config, app_config)
    if err:
        return err

    expired_response = _check_demo_not_expired(store_config, app_config, store_origin)
    if expired_response:
        return expired_response

    if not customer_email:
        return _respond({"reply": HANDOFF_MISSING_EMAIL_MESSAGE, "handoff": False}, 400, store_origin)

    session, err = _get_or_create_session(session_id, store_id, store_origin)
    if err:
        return err

    now = datetime.now(timezone.utc)
    reply = handoff_confirmation_message(store_config)
    user_note = "[Talk to a person] " + summary if summary else "[Talk to a person]"
    db.session.add(Message(session_id=session_id, role="user", content=user_note, ip_address=_client_ip()))
    db.session.add(Message(session_id=session_id, role="assistant", content=reply))
    email_sent = _send_handoff_email_best_effort(app_config, store_config, customer_email, summary, session_id)
    db.session.add(Handoff(session_id=session_id, customer_email=customer_email, summary=summary, email_sent=email_sent))
    session.last_active_at = now
    db.session.commit()

    return _respond({"reply": reply, "handoff": True}, 200, store_origin)
