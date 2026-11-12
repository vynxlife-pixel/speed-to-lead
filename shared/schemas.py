"""Pydantic models and constants shared by the backend and the dashboard."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class Intent(str, Enum):
    BUYER = "Buyer"
    SELLER = "Seller"
    RENTER = "Renter"
    INVESTOR = "Investor"


LEAD_STATUSES = ["New", "Contacted", "Closed"]
INTENTS = [i.value for i in Intent]

DEFAULT_SETTINGS: dict[str, Any] = {
    "agency_name": "Skyline Realty Group",
    "agent_name": "The Skyline Team",
    "agent_email": "",
    "agency_phone": "",
    "agency_website": "",
    "brand_color": "#1F4E79",
    "currency": "USD",
    "target_locations": "Downtown, Riverside, Westfield",
    "min_budget": 250000,
    "hot_lead_threshold": 8,
    "email_signoff": "Warm regards,\nThe Skyline Realty Team",
    "custom_instructions": (
        "Be warm, professional and concise. Never quote prices or promise availability. "
        "Invite the lead to reply with a good time for a call."
    ),
}


class LeadIn(BaseModel):
    """Payload accepted by POST /webhook/new-lead."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    property_interest: str = Field(default="", max_length=300)
    inquiry_message: str = Field(default="", max_length=4000)
    source: str = Field(default="webhook", max_length=60)

    @field_validator("phone", "property_interest", "inquiry_message", "source", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v


class LeadAnalysis(BaseModel):
    """Structured output requested from Gemini. Validators make it tolerant of small model slips."""

    intent: Intent = Field(description="One of Buyer, Seller, Renter, Investor.")
    budget_estimate: str = Field(
        description="Budget as a short human-readable string, e.g. '$450k-$500k'. Use 'Unknown' if not stated."
    )
    budget_max: int = Field(
        description="Upper end of the budget as a whole number in the agency currency. 0 if unknown."
    )
    preferred_location: str = Field(description="Neighborhood or city the lead wants. 'Unknown' if not stated.")
    lead_score: int = Field(description="Lead quality from 1 (cold) to 10 (ready to act now).")
    score_reason: str = Field(description="One short sentence explaining the score.")
    agent_summary: list[str] = Field(description="Exactly two short bullet points for the human agent.")
    email_subject: str = Field(description="Subject line for the instant reply email.")
    email_body: str = Field(
        description="Plain-text email body starting with a greeting. No sign-off. Paragraphs separated by blank lines."
    )

    @field_validator("intent", mode="before")
    @classmethod
    def _normalise_intent(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().title()
        return v

    @field_validator("lead_score", mode="before")
    @classmethod
    def _clamp_score(cls, v: Any) -> int:
        try:
            return max(1, min(10, int(round(float(v)))))
        except (TypeError, ValueError):
            return 5

    @field_validator("budget_max", mode="before")
    @classmethod
    def _clean_budget(cls, v: Any) -> int:
        try:
            return max(0, int(float(v)))
        except (TypeError, ValueError):
            return 0

    @field_validator("agent_summary", mode="before")
    @classmethod
    def _two_bullets(cls, v: Any) -> list[str]:
        items = [v] if isinstance(v, str) else list(v or [])
        items = [str(i).strip().lstrip("-• ").strip() for i in items if str(i).strip()]
        while len(items) < 2:
            items.append("No further detail provided.")
        return items[:2]
