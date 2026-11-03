"""Handoff email sending via SMTP.

If SMTP isn't configured, callers should skip send_handoff_email entirely
and rely on the log-only fallback (see app/agent/handoff.py) — a handoff
must never fail or show an error to the customer just because email isn't
set up yet.
"""

import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape


def is_smtp_configured(app_config) -> bool:
    return bool(app_config.smtp_host and app_config.smtp_from)


def _render_transcript_text(transcript: list[dict]) -> str:
    if not transcript:
        return "(no messages)"
    return "\n".join(f"{m['role']}: {m['content']}" for m in transcript)


def _render_transcript_html(transcript: list[dict]) -> str:
    if not transcript:
        return "<p>(no messages)</p>"
    rows = "".join(
        f"<p><b>{escape(m['role'])}:</b> {escape(m['content'])}</p>" for m in transcript
    )
    return rows


def _short_summary(summary: str, max_len: int = 80) -> str:
    summary = (summary or "").strip() or "a customer question"
    if len(summary) <= max_len:
        return summary
    cut = summary[:max_len].rsplit(" ", 1)[0]
    return (cut or summary[:max_len]).rstrip(" ,.;:-") + "…"


def build_handoff_message(
    *,
    store_name: str,
    support_email: str,
    from_address: str,
    customer_email: str,
    summary: str,
    transcript: list[dict],
    timestamp: datetime | None = None,
) -> MIMEMultipart:
    ts = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    summary_display = summary or "(none provided)"

    text_body = (
        f"New handoff request for {store_name}\n\n"
        f"Customer email: {customer_email}\n"
        f"Summary: {summary_display}\n"
        f"Time: {ts}\n\n"
        f"--- Transcript ---\n"
        f"{_render_transcript_text(transcript)}\n"
    )

    html_body = f"""\
<html><body style="font-family:sans-serif">
<h2>New handoff request for {escape(store_name)}</h2>
<p><b>Customer email:</b> {escape(customer_email)}</p>
<p><b>Summary:</b> {escape(summary_display)}</p>
<p><b>Time:</b> {escape(ts)}</p>
<hr>
<h3>Transcript</h3>
{_render_transcript_html(transcript)}
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Customer question needs you: {_short_summary(summary)}"
    msg["From"] = from_address
    msg["To"] = support_email
    # So the shop owner can just hit reply in their own inbox and have it
    # go straight to the customer, not back to us.
    msg["Reply-To"] = customer_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def _send_message(app_config, msg: MIMEMultipart) -> None:
    """Raises on any failure (bad credentials, network error, etc.) —
    callers must catch this, log it, and continue rather than let it break
    whatever customer/visitor-facing flow triggered the email.
    """
    if app_config.smtp_port == 465:
        server = smtplib.SMTP_SSL(app_config.smtp_host, app_config.smtp_port, timeout=10)
    else:
        server = smtplib.SMTP(app_config.smtp_host, app_config.smtp_port, timeout=10)

    try:
        if app_config.smtp_port != 465:
            server.starttls()
        if app_config.smtp_username:
            server.login(app_config.smtp_username, app_config.smtp_password)
        server.send_message(msg)
    finally:
        server.quit()


def send_handoff_email(
    app_config,
    *,
    store_name: str,
    support_email: str,
    customer_email: str,
    summary: str,
    transcript: list[dict],
) -> None:
    """Raises on any failure (bad credentials, network error, etc.) — callers
    must catch this, log it, and continue; SMTP failure is never shown to
    the customer (see app/agent/handoff.py).
    """
    msg = build_handoff_message(
        store_name=store_name,
        support_email=support_email,
        from_address=app_config.smtp_from,
        customer_email=customer_email,
        summary=summary,
        transcript=transcript,
    )
    _send_message(app_config, msg)


def build_lead_message(
    *,
    to_address: str,
    from_address: str,
    store_url: str,
    email: str,
    name: str,
    timestamp: datetime | None = None,
) -> MIMEMultipart:
    ts = (timestamp or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    name_display = name or "(not given)"

    text_body = (
        f"New demo request from the Tillbird site\n\n"
        f"Store URL: {store_url}\n"
        f"Email: {email}\n"
        f"Name: {name_display}\n"
        f"Time: {ts}\n"
    )
    html_body = f"""\
<html><body style="font-family:sans-serif">
<h2>New demo request from the Tillbird site</h2>
<p><b>Store URL:</b> {escape(store_url)}</p>
<p><b>Email:</b> {escape(email)}</p>
<p><b>Name:</b> {escape(name_display)}</p>
<p><b>Time:</b> {escape(ts)}</p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"New demo request: {store_url}"
    msg["From"] = from_address
    msg["To"] = to_address
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def build_alert_message(*, to_address: str, from_address: str, subject: str, body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.attach(MIMEText(body, "plain"))
    return msg


def send_alert_email(app_config, *, to_address: str, subject: str, body: str) -> None:
    """A plain-text internal ops notification (usage-threshold alerts,
    trial-ended notices — see app.usage_limits/app.store_status) — always
    to me, never the shop owner. Raises on any SMTP failure; callers must
    catch, log, and continue (never break the customer-facing chat flow
    that happened to trigger the check).
    """
    msg = build_alert_message(to_address=to_address, from_address=app_config.smtp_from, subject=subject, body=body)
    _send_message(app_config, msg)


def build_weekly_summary_message(*, to_address: str, from_address: str, store_name: str, html_body: str, text_body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{store_name}: your weekly Tillbird summary"
    msg["From"] = from_address
    msg["To"] = to_address
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_weekly_summary_email(app_config, *, to_address: str, store_name: str, html_body: str, text_body: str) -> None:
    """Raises on any failure — callers (scripts/weekly_summary.py) must
    catch, log, and continue.
    """
    msg = build_weekly_summary_message(
        to_address=to_address, from_address=app_config.smtp_from, store_name=store_name, html_body=html_body, text_body=text_body
    )
    _send_message(app_config, msg)


def send_lead_email(app_config, *, to_address: str, store_url: str, email: str, name: str) -> None:
    """Raises on any failure — callers must catch, log, and continue; a
    failed notification must never break the visitor-facing form submit
    (see app/routes/leads.py, which always stores the lead in the DB first).
    """
    msg = build_lead_message(
        to_address=to_address,
        from_address=app_config.smtp_from,
        store_url=store_url,
        email=email,
        name=name,
    )
    _send_message(app_config, msg)
