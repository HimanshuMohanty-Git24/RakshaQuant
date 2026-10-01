"""
The prompt templates and their output schemas (plan M6): ``veto_v1`` (Book C, online),
``review_v1`` (nightly post-trade review), ``explain_v1`` (trade narratives) and
``label_announcement_v1`` (teacher labels for decision-model calibration, M7).

Changing a template's text changes its hash (``prompt_version``); bump ``version`` when the
change is meant to be compared with the old one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.llm.prompts.base import PromptTemplate

# -- output schemas (extra fields are ignored: no price/quantity/stop/target can get through) --


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    @field_validator("schema_version", check_fields=False)
    @classmethod
    def _version_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"schema_version must be 1, got {value}")
        return value


class EvidenceClaim(_Output):
    claim: str = Field(max_length=300)
    evidence_ref: str = Field(max_length=120, description="A key path from the input data")


class VetoOutput(_Output):
    verdict: Literal["APPROVE", "VETO", "ABSTAIN"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[EvidenceClaim] = Field(max_length=5)
    schema_version: int


class ReviewOutput(_Output):
    summary: str = Field(max_length=1200)
    what_worked: list[str] = Field(max_length=5)
    what_failed: list[str] = Field(max_length=5)
    lessons: list[EvidenceClaim] = Field(max_length=5)
    schema_version: int


class ExplainOutput(_Output):
    explanation: str = Field(max_length=1500)
    schema_version: int


EventType = Literal[
    "results", "results_date", "dividend", "split_bonus", "pledge", "insider_or_promoter",
    "order_win", "litigation_or_regulatory", "management_change", "rating_change", "fundraise",
    "other",
]  # fmt: skip


class AnnouncementLabel(_Output):
    relevant: bool
    event_type: EventType
    direction: Literal["positive", "negative", "neutral", "unclear"]
    materiality: Literal["minor", "moderate", "major"]
    confidence: float = Field(ge=0, le=1)
    schema_version: int


# -- templates ---------------------------------------------------------------------------------

VETO_V1 = PromptTemplate(
    name="veto",
    version=1,
    system=(
        "You review one proposed trade for an NSE (India) cash-equity swing-trading book. A "
        "deterministic strategy has proposed OPENING A LONG position; position size, entry, stop "
        "and target are fixed by deterministic rules and are not yours to change.\n"
        "Answer APPROVE, VETO or ABSTAIN:\n"
        "- VETO only for a concrete adverse condition that is present in the input (for example "
        "a scheduled results announcement, a recent negative material event, an extreme reading "
        "in the features) and that makes opening this long today imprudent.\n"
        "- APPROVE when the input shows no such condition.\n"
        "- ABSTAIN when the input is insufficient to judge.\n"
        "Every reason must cite evidence_ref: the exact key path of the input it relies on, such "
        "as signal.agreement_score, features.rsi_14 or events[0]. A reason without a real "
        "reference invalidates the whole answer. Do not invent facts that are not in the input. "
        "confidence is your probability that the verdict is right."
    ),
    instructions=(
        "Return one JSON object: verdict (APPROVE|VETO|ABSTAIN), confidence (0 to 1), reasons "
        "(at most 5 objects with claim and evidence_ref), schema_version (always 1)."
    ),
)

REVIEW_V1 = PromptTemplate(
    name="review",
    version=1,
    system=(
        "You review the day's closed paper trades of an NSE cash-equity swing-trading system. "
        "Explain what worked and what failed using only the trades, decisions and market context "
        "in the input. Lessons must cite evidence_ref key paths from the input. You never change "
        "configuration; your output is read by a human."
    ),
    instructions=(
        "Return one JSON object: summary, what_worked (list), what_failed (list), lessons (at "
        "most 5 objects with claim and evidence_ref), schema_version (always 1)."
    ),
)

EXPLAIN_V1 = PromptTemplate(
    name="explain",
    version=1,
    system=(
        "You explain one trade decision of an NSE cash-equity paper-trading system to its owner "
        "in plain English: why the signal fired, what the risk checks decided and why, and how "
        "the trade ended. Use only facts in the input; say so when something is not known."
    ),
    instructions="Return one JSON object: explanation (at most 1500 characters), schema_version (1).",
)

LABEL_ANNOUNCEMENT_V1 = PromptTemplate(
    name="label_announcement",
    version=1,
    system=(
        "You label NSE corporate announcements for a typed-event dataset. For the given company "
        "and announcement decide: is it relevant to the stock's price (relevant); its event_type; "
        "its likely price direction for the company's shareholders; and its materiality. Judge "
        "from the announcement text alone."
    ),
    instructions=(
        "Return one JSON object: relevant (true|false), event_type (results, results_date, "
        "dividend, split_bonus, pledge, insider_or_promoter, order_win, litigation_or_regulatory, "
        "management_change, rating_change, fundraise, other), direction (positive|negative|"
        "neutral|unclear), materiality (minor|moderate|major), confidence (0 to 1), "
        "schema_version (always 1)."
    ),
)

TEMPLATES: dict[str, PromptTemplate] = {
    t.id: t for t in (VETO_V1, REVIEW_V1, EXPLAIN_V1, LABEL_ANNOUNCEMENT_V1)
}
