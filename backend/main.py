"""FastAPI app: secure webhook intake + background lead-processing pipeline.

Run from the repository root:
    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from backend import ai_engine, services
from shared.config import get_config
from shared.schemas import LeadIn

cfg = get_config()
logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("speed_to_lead.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting Speed-to-Lead API (%s)", cfg.environment)
    logger.info(
        "Integrations -> Gemini: %s | Supabase: %s | Resend: %s | Slack: %s | SMS: %s",
        "LIVE" if cfg.gemini_api_key else "mock",
        "LIVE" if services.get_supabase() else "mock (memory)",
        "LIVE" if cfg.resend_api_key else "mock",
        "LIVE" if cfg.slack_webhook_url else "mock",
        "LIVE" if cfg.twilio_enabled else "off",
    )
    if not cfg.webhook_api_key:
        logger.warning("WEBHOOK_API_KEY is not set: the webhook is UNPROTECTED. Set it before going live.")
    yield


app = FastAPI(
    title="Real Estate Speed-to-Lead Engine",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if cfg.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if cfg.is_production else "/openapi.json",
)

if cfg.allowed_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=cfg.allowed_origins, allow_methods=["POST", "GET"], allow_headers=["*"]
    )


# --------------------------------------------------------------------------------------
# Security: API key + simple per-IP rate limit
# --------------------------------------------------------------------------------------
_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    now = time.monotonic()
    window = _hits[_client_ip(request)]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= cfg.rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    window.append(now)


async def verify_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = cfg.webhook_api_key
    if not expected:
        if cfg.is_production:
            raise HTTPException(status_code=503, detail="Server is not configured: WEBHOOK_API_KEY missing.")
        return  # local development only
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


# --------------------------------------------------------------------------------------
# Background pipeline
# --------------------------------------------------------------------------------------
def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return cleaned[:40] or "Property-Guide"


async def process_lead(lead_id: str, lead: LeadIn, created_at: datetime) -> None:
    """AI analysis -> save -> PDF -> email -> hot-lead alert. Each stage is isolated so one failure
    never blocks the others; problems are recorded on the lead row for the dashboard."""
    errors: list[str] = []
    try:
        settings = await asyncio.to_thread(services.get_agency_settings)

        # 1. AI qualification
        analysis, ai_mode = await ai_engine.analyze_lead(lead, settings)
        try:
            await asyncio.to_thread(
                services.update_lead,
                lead_id,
                {
                    "intent": analysis.intent.value,
                    "budget_estimate": analysis.budget_estimate,
                    "budget_max": analysis.budget_max,
                    "preferred_location": analysis.preferred_location,
                    "lead_score": analysis.lead_score,
                    "score_reason": analysis.score_reason,
                    "summary": analysis.agent_summary,
                    "email_subject": analysis.email_subject,
                    "email_body": analysis.email_body,
                    "ai_mode": ai_mode,
                    "processing_status": "analyzed",
                },
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"save-analysis: {exc}")
            logger.exception("Could not save analysis for lead %s", lead_id)

        # 2. PDF brochure
        pdf_bytes: bytes | None = None
        try:
            pdf_bytes = await asyncio.to_thread(services.generate_brochure_pdf, lead.full_name, settings, analysis)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pdf: {exc}")
            logger.exception("PDF generation failed for lead %s", lead_id)

        # 3. Instant email
        email_status, email_error = await services.send_lead_email(
            lead_id=lead_id,
            to_email=str(lead.email),
            subject=analysis.email_subject,
            body=analysis.email_body,
            settings=settings,
            pdf_bytes=pdf_bytes,
            pdf_name=f"{_safe_filename(settings.get('agency_name', 'Property'))}-Guide.pdf",
        )
        if email_error:
            errors.append(email_error)
        update: dict = {"email_status": email_status}
        if email_status in ("sent", "mocked"):
            sent_at = datetime.now(timezone.utc)
            update["email_sent_at"] = sent_at.isoformat()
            update["response_seconds"] = round((sent_at - created_at).total_seconds(), 1)
        try:
            await asyncio.to_thread(services.update_lead, lead_id, update)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"save-email-status: {exc}")

        # 4. Hot lead alert
        try:
            threshold = int(settings.get("hot_lead_threshold") or 8)
        except (TypeError, ValueError):
            threshold = 8
        if analysis.lead_score >= threshold:
            channels, alert_error = await services.send_hot_lead_alert(lead, analysis, settings)
            if alert_error:
                errors.append(alert_error)
            await asyncio.to_thread(
                services.update_lead, lead_id, {"alert_sent": channels not in ("failed",), "alert_channels": channels}
            )

        logger.info(
            "Lead %s processed: score=%s intent=%s email=%s ai=%s",
            lead_id, analysis.lead_score, analysis.intent.value, email_status, ai_mode,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pipeline: {exc}")
        logger.exception("Pipeline crashed for lead %s", lead_id)
    finally:
        try:
            await asyncio.to_thread(
                services.update_lead,
                lead_id,
                {
                    "processing_status": "completed_with_errors" if errors else "complete",
                    "error_log": "\n".join(errors)[:2000] if errors else None,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not write final status for lead %s", lead_id)


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------
@app.get("/")
async def root() -> dict:
    return {"service": "Real Estate Speed-to-Lead Engine", "status": "ok"}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": cfg.environment,
        "integrations": {
            "gemini": bool(cfg.gemini_api_key),
            "supabase": services.get_supabase() is not None,
            "resend": bool(cfg.resend_api_key),
            "slack": bool(cfg.slack_webhook_url),
            "sms": cfg.twilio_enabled,
            "webhook_auth": bool(cfg.webhook_api_key),
        },
    }


@app.post("/webhook/new-lead", status_code=202, dependencies=[Depends(rate_limit), Depends(verify_api_key)])
async def new_lead(payload: LeadIn, background_tasks: BackgroundTasks) -> dict:
    """Save the lead immediately (so it is never lost), then qualify and respond in the background."""
    try:
        row = await asyncio.to_thread(services.insert_lead, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not store incoming lead")
        # 503 makes most form tools (Tally, Zapier, Make) retry automatically.
        raise HTTPException(status_code=503, detail="Could not store lead. Please retry.") from exc

    lead_id = str(row["id"])
    background_tasks.add_task(process_lead, lead_id, payload, services.parse_ts(row.get("created_at")))
    return {"status": "accepted", "lead_id": lead_id, "message": "Lead received. Processing started."}
