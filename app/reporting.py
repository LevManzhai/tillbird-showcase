"""Monthly usage report for a store (scripts/report.py).

Reads sessions/messages/tool_calls/handoffs from SQLite; asks Claude to
categorise a sample of customer messages into topics (there's no reliable
non-LLM way to do this well); renders everything as Markdown.
"""

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

from app.llm import create_with_tool
from app.models import Handoff, Message, Session, ToolCall

CATEGORIZE_TOPICS_TOOL = {
    "name": "categorize_topics",
    "description": "Group customer messages into common support topics and count how many fall into each.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "Short topic label, e.g. 'Order status'"},
                        "count": {"type": "integer"},
                    },
                    "required": ["topic", "count"],
                },
            }
        },
        "required": ["topics"],
    },
}


@dataclass
class MonthlyReport:
    store_id: str
    month: str
    total_conversations: int
    total_messages: int
    resolved_without_handoff_pct: float
    handoff_count: int
    order_lookup_count: int
    topics: list[dict] = field(default_factory=list)
    example_conversations: list[list[dict]] = field(default_factory=list)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(part) for part in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, mon)[1]
    end = datetime(year, mon, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return start, end


def categorize_topics(app_config, sample_messages: list[str], client=None) -> list[dict]:
    if not sample_messages:
        return []
    client = client or anthropic.Anthropic(api_key=app_config.anthropic_api_key)
    prompt = (
        "Group these customer support messages into a handful of common topics "
        "(e.g. 'Order status', 'Shipping', 'Returns', 'Sizing', 'Other') and count how many "
        "fall into each. Every message should count toward exactly one topic.\n\n"
        + "\n".join(f"- {m}" for m in sample_messages)
    )
    response = create_with_tool(
        client,
        tool_name="categorize_topics",
        model=app_config.anthropic_model,
        max_tokens=1000,
        tools=[CATEGORIZE_TOPICS_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "categorize_topics":
            topics = block.input.get("topics", [])
            return sorted(topics, key=lambda t: -t.get("count", 0))
    return []


def generate_monthly_report(
    db_session, app_config, store_id: str, month: str, client=None, sample_size: int = 30, example_count: int = 5
) -> MonthlyReport:
    start, end = month_bounds(month)

    sessions = (
        db_session.query(Session)
        .filter(Session.store_id == store_id, Session.created_at >= start, Session.created_at <= end)
        .order_by(Session.created_at)
        .all()
    )
    session_ids = [s.id for s in sessions]
    total_conversations = len(sessions)

    messages_by_session: dict[str, list[Message]] = {sid: [] for sid in session_ids}
    total_messages = 0
    if session_ids:
        messages = (
            db_session.query(Message)
            .filter(Message.session_id.in_(session_ids))
            .order_by(Message.created_at)
            .all()
        )
        total_messages = len(messages)
        for m in messages:
            messages_by_session.setdefault(m.session_id, []).append(m)

    handoff_session_ids: set[str] = set()
    handoff_count = 0
    if session_ids:
        handoffs = db_session.query(Handoff).filter(Handoff.session_id.in_(session_ids)).all()
        handoff_count = len(handoffs)
        handoff_session_ids = {h.session_id for h in handoffs}

    resolved_without_handoff_pct = (
        100.0 * (total_conversations - len(handoff_session_ids)) / total_conversations
        if total_conversations
        else 0.0
    )

    order_lookup_count = 0
    if session_ids:
        order_lookup_count = (
            db_session.query(ToolCall)
            .filter(ToolCall.session_id.in_(session_ids), ToolCall.name == "lookup_order")
            .count()
        )

    user_messages = [
        m.content for sid in session_ids for m in messages_by_session.get(sid, []) if m.role == "user"
    ]
    topics = categorize_topics(app_config, user_messages[:sample_size], client=client)

    example_conversations = [
        [{"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in messages_by_session[sid]]
        for sid in session_ids[:example_count]
    ]

    return MonthlyReport(
        store_id=store_id,
        month=month,
        total_conversations=total_conversations,
        total_messages=total_messages,
        resolved_without_handoff_pct=resolved_without_handoff_pct,
        handoff_count=handoff_count,
        order_lookup_count=order_lookup_count,
        topics=topics,
        example_conversations=example_conversations,
    )


def render_markdown(report: MonthlyReport) -> str:
    lines = [
        f"# {report.store_id} — {report.month}",
        "",
        f"- **Total conversations**: {report.total_conversations}",
        f"- **Total messages**: {report.total_messages}",
        f"- **Resolved without handoff**: {report.resolved_without_handoff_pct:.1f}%",
        f"- **Handoffs**: {report.handoff_count}",
        f"- **Order lookups**: {report.order_lookup_count}",
        "",
        "## Top question topics",
        "",
    ]
    if report.topics:
        for t in report.topics:
            lines.append(f"- {t['topic']}: {t['count']}")
    else:
        lines.append("(no messages this month)")
    lines.append("")
    lines.append("## Example conversations")
    lines.append("")
    if not report.example_conversations:
        lines.append("(none)")
    for i, convo in enumerate(report.example_conversations, 1):
        lines.append(f"### Conversation {i}")
        lines.append("")
        for m in convo:
            lines.append(f"**{m['role']}** ({m['created_at']}): {m['content']}")
            lines.append("")
    return "\n".join(lines)
