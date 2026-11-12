"""Central configuration, read once from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

# Values copied straight from .env.example placeholders are treated as "not set".
_PLACEHOLDER_PREFIXES = ("your_", "your-", "paste_", "changeme", "xxx")


def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name) or default).strip()
    if value.lower().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return value


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    environment: str = "development"
    log_level: str = "INFO"

    # AI
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Database
    supabase_url: str = ""
    supabase_service_key: str = ""

    # Webhook security
    webhook_api_key: str = ""
    rate_limit_per_minute: int = 60
    allowed_origins: list[str] = field(default_factory=list)

    # Email (Resend)
    resend_api_key: str = ""
    email_from: str = "Speed-to-Lead <onboarding@resend.dev>"

    # Agent alerts
    slack_webhook_url: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    agent_alert_phone: str = ""

    # Dashboard
    backend_url: str = "http://localhost:8000"
    dashboard_password: str = ""
    display_timezone: str = "UTC"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def twilio_enabled(self) -> bool:
        return all(
            [self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_number, self.agent_alert_phone]
        )


@lru_cache(maxsize=1)
def get_config() -> Config:
    origins = [o.strip() for o in _env("ALLOWED_ORIGINS").split(",") if o.strip()]
    return Config(
        environment=_env("ENVIRONMENT", "development"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        gemini_api_key=_env("GEMINI_API_KEY"),
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        supabase_url=_env("SUPABASE_URL"),
        supabase_service_key=_env("SUPABASE_SERVICE_KEY"),
        webhook_api_key=_env("WEBHOOK_API_KEY"),
        rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", 60),
        allowed_origins=origins,
        resend_api_key=_env("RESEND_API_KEY"),
        email_from=_env("EMAIL_FROM", "Speed-to-Lead <onboarding@resend.dev>"),
        slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
        twilio_account_sid=_env("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
        twilio_from_number=_env("TWILIO_FROM_NUMBER"),
        agent_alert_phone=_env("AGENT_ALERT_PHONE"),
        backend_url=_env("BACKEND_URL", "http://localhost:8000").rstrip("/"),
        dashboard_password=_env("DASHBOARD_PASSWORD"),
        display_timezone=_env("DISPLAY_TIMEZONE", "UTC"),
    )
