"""Integrations: Supabase CRUD, PDF brochure, Resend email, Slack/SMS alerts.

Every integration has a safe mock fallback so the app runs locally with zero API keys.
"""
from __future__ import annotations

import base64
import html
import logging
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import httpx
from fpdf import FPDF

from shared.config import get_config
from shared.schemas import DEFAULT_SETTINGS, LeadAnalysis, LeadIn

logger = logging.getLogger("speed_to_lead.services")

# In-memory stores used only when Supabase is not configured (local demo mode).
_MEM_LEADS: dict[str, dict[str, Any]] = {}
_MEM_SETTINGS: dict[str, Any] = dict(DEFAULT_SETTINGS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_supabase():
    cfg = get_config()
    if not (cfg.supabase_url and cfg.supabase_service_key):
        return None
    from supabase import create_client

    return create_client(cfg.supabase_url, cfg.supabase_service_key)


def get_agency_settings() -> dict[str, Any]:
    """Settings from the database merged over safe defaults (so a missing column never breaks a lead)."""
    merged = dict(DEFAULT_SETTINGS)
    client = get_supabase()
    if client is None:
        merged.update(_MEM_SETTINGS)
        return merged
    try:
        res = client.table("agency_settings").select("*").eq("id", 1).limit(1).execute()
        if res.data:
            merged.update({k: v for k, v in res.data[0].items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not load agency settings, using defaults: %s", exc)
    return merged


def insert_lead(lead: LeadIn) -> dict[str, Any]:
    row = {
        "full_name": lead.full_name,
        "email": str(lead.email),
        "phone": lead.phone,
        "property_interest": lead.property_interest,
        "inquiry_message": lead.inquiry_message,
        "source": lead.source,
        "status": "New",
        "processing_status": "pending",
    }
    client = get_supabase()
    if client is None:
        row.update({"id": str(uuid.uuid4()), "created_at": now_iso()})
        _MEM_LEADS[row["id"]] = row
        logger.warning("[MOCK DB] Lead stored in memory only: %s", row["id"])
        return row
    res = client.table("leads").insert(row).execute()
    return res.data[0]


def update_lead(lead_id: str, fields: dict[str, Any]) -> None:
    client = get_supabase()
    if client is None:
        _MEM_LEADS.get(lead_id, {}).update(fields)
        return
    client.table("leads").update(fields).eq("id", lead_id).execute()


# --------------------------------------------------------------------------------------
# PDF brochure
# --------------------------------------------------------------------------------------
def _latin(text: Any) -> str:
    """Built-in PDF fonts only support Latin-1; replace anything else instead of crashing."""
    return str(text or "").encode("latin-1", "replace").decode("latin-1")


def _hex_to_rgb(value: str, default: tuple[int, int, int] = (31, 78, 121)) -> tuple[int, int, int]:
    v = (value or "").lstrip("#")
    if len(v) == 6:
        try:
            return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        except ValueError:
            pass
    return default


def generate_brochure_pdf(lead_name: str, settings: dict[str, Any], analysis: LeadAnalysis | None = None) -> bytes:
    """Create a clean, branded, strictly one-page A4 brochure."""
    r, g, b = _hex_to_rgb(settings.get("brand_color", ""))
    agency = settings.get("agency_name", "Our Agency")
    locations = [x.strip() for x in str(settings.get("target_locations", "")).split(",") if x.strip()][:8]

    pdf = FPDF(unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # Header band
    pdf.set_fill_color(r, g, b)
    pdf.rect(0, 0, 210, 52, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_xy(15, 16)
    pdf.cell(180, 12, _latin(agency))
    pdf.set_font("Helvetica", "", 12)
    pdf.set_xy(15, 31)
    pdf.cell(180, 8, "Your personalised property guide")

    # Greeting
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(15, 66)
    pdf.cell(180, 10, _latin(f"Prepared for {lead_name}"))
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(70, 70, 70)
    pdf.set_xy(15, 80)
    intro = (
        f"Thank you for getting in touch with {agency}. Whether you are buying, selling, renting or "
        "investing, our local team combines market knowledge with fast, honest communication. "
        "Here is a quick overview of how we can help you."
    )
    pdf.multi_cell(180, 6, _latin(intro), new_x="LMARGIN", new_y="NEXT")

    # Areas we cover
    y = pdf.get_y() + 8
    pdf.set_text_color(r, g, b)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_xy(15, y)
    pdf.cell(180, 8, "Areas we specialise in")
    pdf.set_text_color(40, 40, 40)
    pdf.set_font("Helvetica", "", 11)
    y += 12
    for i, loc in enumerate(locations or ["Ask us about your preferred area"]):
        col_x = 15 if i % 2 == 0 else 110
        row_y = y + (i // 2) * 8
        pdf.set_fill_color(r, g, b)
        pdf.ellipse(col_x, row_y + 2, 3, 3, "F")
        pdf.set_xy(col_x + 6, row_y)
        pdf.cell(80, 7, _latin(loc))
    y += ((len(locations) + 1) // 2 or 1) * 8 + 8

    # What happens next
    pdf.set_text_color(r, g, b)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_xy(15, y)
    pdf.cell(180, 8, "What happens next")
    y += 12
    steps = [
        ("Personal follow-up", "A dedicated agent reviews your enquiry and contacts you shortly."),
        ("Tailored options", "We shortlist properties or strategies matched to your goals and budget."),
        ("Clear next steps", "Viewings, valuations or offers, handled with full transparency."),
    ]
    for n, (title, text) in enumerate(steps, start=1):
        pdf.set_fill_color(r, g, b)
        pdf.rect(15, y, 10, 10, "F")
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_xy(15, y + 1.5)
        pdf.cell(10, 7, str(n), align="C")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_xy(30, y - 0.5)
        pdf.cell(165, 6, _latin(title))
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(90, 90, 90)
        pdf.set_xy(30, y + 5.5)
        pdf.cell(165, 5, _latin(text))
        y += 18

    # Contact box
    box_y = 232
    pdf.set_fill_color(242, 245, 249)
    pdf.rect(15, box_y, 180, 40, "F")
    pdf.set_fill_color(r, g, b)
    pdf.rect(15, box_y, 3, 40, "F")
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(24, box_y + 5)
    pdf.cell(165, 7, _latin(f"Talk to {settings.get('agent_name') or agency}"))
    pdf.set_font("Helvetica", "", 11)
    lines = [
        settings.get("agency_phone"),
        settings.get("agent_email"),
        settings.get("agency_website"),
    ]
    ly = box_y + 14
    for line in [l for l in lines if l] or ["Simply reply to the email this brochure came with."]:
        pdf.set_xy(24, ly)
        pdf.cell(165, 6, _latin(line))
        ly += 7

    # Footer
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(140, 140, 140)
    pdf.set_xy(15, 282)
    pdf.cell(180, 5, _latin(f"Prepared for {lead_name} on {datetime.now().strftime('%d %B %Y')}. For information only; not an offer."), align="C")

    return bytes(pdf.output())


# --------------------------------------------------------------------------------------
# Email via Resend
# --------------------------------------------------------------------------------------
def render_email_html(body: str, settings: dict[str, Any]) -> str:
    color = settings.get("brand_color") or "#1F4E79"
    if _hex_to_rgb(color, (-1, -1, -1)) == (-1, -1, -1):
        color = "#1F4E79"
    paragraphs = "".join(
        f'<p style="margin:0 0 16px;line-height:1.6;">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in [p.strip() for p in body.split("\n\n")] if p
    )
    signoff = html.escape(settings.get("email_signoff") or "").replace("\n", "<br>")
    contact = " &nbsp;|&nbsp; ".join(
        html.escape(str(x)) for x in (settings.get("agency_phone"), settings.get("agency_website")) if x
    )
    return f"""<!doctype html><html><body style="margin:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#222;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border-radius:8px;overflow:hidden;">
<tr><td style="background:{color};padding:22px 28px;color:#fff;font-size:20px;font-weight:bold;">{html.escape(settings.get("agency_name", ""))}</td></tr>
<tr><td style="padding:28px;font-size:15px;">{paragraphs}<p style="margin:24px 0 0;line-height:1.6;">{signoff}</p></td></tr>
<tr><td style="padding:16px 28px;background:#f4f6f8;font-size:12px;color:#777;">{contact}</td></tr>
</table></td></tr></table></body></html>"""


async def send_lead_email(
    *, lead_id: str, to_email: str, subject: str, body: str, settings: dict[str, Any], pdf_bytes: bytes | None, pdf_name: str
) -> tuple[str, str | None]:
    """Send the instant reply. Returns (status, error). status: sent | mocked | failed."""
    cfg = get_config()
    html_body = render_email_html(body, settings)

    if not cfg.resend_api_key:
        logger.warning(
            "[MOCK EMAIL] To: %s | Subject: %s | Attachment: %s (%s bytes)\n%s",
            to_email, subject, pdf_name, len(pdf_bytes or b""), body,
        )
        return "mocked", None

    payload: dict[str, Any] = {"from": cfg.email_from, "to": [to_email], "subject": subject, "html": html_body, "text": body}
    if settings.get("agent_email"):
        payload["reply_to"] = settings["agent_email"]
    if pdf_bytes:
        payload["attachments"] = [{"filename": pdf_name, "content": base64.b64encode(pdf_bytes).decode()}]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {cfg.resend_api_key}", "Idempotency-Key": f"lead-{lead_id}"},
            )
        if resp.status_code >= 300:
            return "failed", f"Resend {resp.status_code}: {resp.text[:300]}"
        return "sent", None
    except Exception as exc:  # noqa: BLE001
        return "failed", f"Resend error: {exc}"


# --------------------------------------------------------------------------------------
# Hot lead alerts (Slack + optional Twilio SMS)
# --------------------------------------------------------------------------------------
async def send_hot_lead_alert(lead: LeadIn, analysis: LeadAnalysis, settings: dict[str, Any]) -> tuple[str, str | None]:
    """Returns (channels, error). channels is a comma list such as 'slack,sms' or 'mock'."""
    cfg = get_config()
    channels: list[str] = []
    errors: list[str] = []
    title = f"HOT LEAD ({analysis.lead_score}/10): {lead.full_name}"
    summary = "\n".join(f"• {s}" for s in analysis.agent_summary)
    excerpt = (lead.inquiry_message[:300] + "...") if len(lead.inquiry_message) > 300 else lead.inquiry_message

    if not cfg.slack_webhook_url and not cfg.twilio_enabled:
        logger.warning(
            "[MOCK ALERT] %s | %s | %s | %s | budget %s | %s\n%s",
            title, analysis.intent.value, lead.email, lead.phone or "no phone",
            analysis.budget_estimate, analysis.preferred_location, summary,
        )
        return "mock", None

    async with httpx.AsyncClient(timeout=20) as client:
        if cfg.slack_webhook_url:
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"🔥 {title}"[:150]}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Intent:*\n{analysis.intent.value}"},
                    {"type": "mrkdwn", "text": f"*Budget:*\n{analysis.budget_estimate}"},
                    {"type": "mrkdwn", "text": f"*Location:*\n{analysis.preferred_location}"},
                    {"type": "mrkdwn", "text": f"*Phone:*\n{lead.phone or 'n/a'}"},
                    {"type": "mrkdwn", "text": f"*Email:*\n{lead.email}"},
                    {"type": "mrkdwn", "text": f"*Interest:*\n{lead.property_interest or 'n/a'}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Summary*\n{summary}\n\n*Why:* {analysis.score_reason}"}},
            ]
            if excerpt:
                blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{excerpt}_"}]})
            try:
                resp = await client.post(cfg.slack_webhook_url, json={"text": title, "blocks": blocks})
                if resp.status_code < 300:
                    channels.append("slack")
                else:
                    errors.append(f"Slack {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Slack error: {exc}")

        if cfg.twilio_enabled:
            sms = f"{title}\n{analysis.intent.value} | {analysis.budget_estimate} | {analysis.preferred_location}\nCall: {lead.phone or lead.email}"
            try:
                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{cfg.twilio_account_sid}/Messages.json",
                    auth=(cfg.twilio_account_sid, cfg.twilio_auth_token),
                    data={"To": cfg.agent_alert_phone, "From": cfg.twilio_from_number, "Body": sms[:600]},
                )
                if resp.status_code < 300:
                    channels.append("sms")
                else:
                    errors.append(f"Twilio {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Twilio error: {exc}")

    return ",".join(channels) or "failed", "; ".join(errors) or None
