"""
The announcement classifier (plan M7.7): every new ``AnnouncementReceived`` becomes a
``TypedEvent`` (projected into ``typed_events``) for the event gate (M7.8) and Book B (M7.9).

* **State** - built only from the public announcement (company, the exchange's subject, the
  text, cut to fit the checkpoint's context): :func:`announcement_state` takes an
  ``AnnouncementReceived`` and nothing else, so no portfolio data can reach a decision model.
* **Questions** - ``relevant`` (noul), ``event_type`` (12-way choice), ``direction`` (4-way
  choice), ``materiality`` (score: minor < moderate < major), answered by the Laya→Jev cascade.
* **Exchange categories first** - where NSE's own ``SUBJECT`` is unambiguous (financial results,
  a board meeting to consider results, dividend, split/bonus, pledge, insider trading) it sets
  the event type deterministically (``extra.type_source = "exchange_subject"``), because the
  event gate must not depend on an uncalibrated model to see a results date. The model still
  answers direction and materiality, and the type of everything else.
* **Dates** - the date a scheduled event happens (the board meeting that considers results) is
  extracted from the text deterministically into ``extra.event_date``.
* A question the cascade abstains on stays ``None`` (unknown), never a guess.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Protocol, TypeVar

from pydantic import JsonValue

from src.decision_models.base import Answer, Question
from src.decision_models.cascade import Cascade, CascadeResult
from src.domain.clock import Clock
from src.domain.events import AnnouncementReceived
from src.domain.sink import EventSink
from src.domain.types import AnnouncementType, EventDirection, Materiality, TypedEvent
from src.marketdata.announcements import PollStats
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

TASK = "announcements"
EVENT_TYPES: Mapping[str, str] = {
    "results": "the company reports its financial results (quarterly or annual)",
    "results_date": "the company announces the date of a board meeting to consider results",
    "dividend": "a dividend is declared, recommended or its record date is set",
    "split_bonus": "a stock split or bonus issue",
    "pledge": "promoter shares are pledged, released or encumbered",
    "insider_or_promoter": "insiders or promoters buy or sell shares",
    "order_win": "the company wins a contract or order",
    "litigation_or_regulatory": "a lawsuit, penalty, regulatory action or investigation",
    "management_change": "a director, CEO, CFO or auditor joins, resigns or is replaced",
    "rating_change": "a credit rating is assigned, upgraded or downgraded",
    "fundraise": "shares, debentures or other securities are issued to raise money",
    "other": "anything else",
}
QUESTIONS: Mapping[str, Question] = {
    "relevant": Question("noul", "Could this announcement move the company's share price?",
                         {"false": "routine or immaterial filing", "true": "price-relevant news"}),
    "event_type": Question("choice", "What kind of corporate event is this?", dict(EVENT_TYPES)),
    "direction": Question("choice", "What is the likely effect on the share price?",
                          {"positive": "good news for shareholders",
                           "negative": "bad news for shareholders",
                           "neutral": "no material effect", "unclear": "cannot tell"}),
    "materiality": Question("score", "How material is this event for the company?",
                            ("minor", "moderate", "major")),
}  # fmt: skip
MATERIALITY = ("minor", "moderate", "major")

_SUBJECT_RULES: tuple[tuple[re.Pattern[str], AnnouncementType], ...] = (
    (re.compile(r"board meeting", re.I), AnnouncementType.RESULTS_DATE),  # refined below
    (re.compile(r"financial result|integrated filing.*financial", re.I), AnnouncementType.RESULTS),
    (re.compile(r"dividend|record date", re.I), AnnouncementType.DIVIDEND),
    (re.compile(r"\bbonus\b|sub-?division|split", re.I), AnnouncementType.SPLIT_BONUS),
    (re.compile(r"pledge|encumbrance", re.I), AnnouncementType.PLEDGE),
    (re.compile(r"insider trading|sast", re.I), AnnouncementType.INSIDER_OR_PROMOTER),
)
_RESULTS_WORDS = re.compile(r"financial results|audited|unaudited|quarter(ly)? results", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}  # fmt: skip
_DATE_PATTERNS = (
    re.compile(r"\b(?P<m>[A-Za-z]{3,9})\.? (?P<d>\d{1,2}),? (?P<y>20\d\d)\b"),  # October 16, 2026
    re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)? (?P<m>[A-Za-z]{3,9}),? (?P<y>20\d\d)\b"),
    re.compile(r"\b(?P<d>\d{1,2})-(?P<m>[A-Za-z]{3})-(?P<y>20\d\d)\b"),  # 16-Oct-2026
    re.compile(r"\b(?P<d>\d{1,2})[/.](?P<mn>\d{1,2})[/.](?P<y>20\d\d)\b"),  # 16/10/2026
)


def announcement_state(
    announcement: AnnouncementReceived, max_tokens: int = 1024
) -> dict[str, str]:
    """The decision-model state: public announcement fields only, cut to fit the context."""
    budget = max(64, max_tokens - 96) * 4  # ≈4 characters per token, room for the questions
    post = announcement.title if len(announcement.title) <= budget else announcement.title[:budget]
    state = {"instrument": announcement.company, "post": post}
    if announcement.subject:
        state["subject"] = announcement.subject
    return state


def subject_type(subject: str, text: str) -> AnnouncementType | None:
    """The event type NSE's own category makes unambiguous (None = ask the model)."""
    for pattern, kind in _SUBJECT_RULES:
        if pattern.search(subject):
            if kind is AnnouncementType.RESULTS_DATE:
                return kind if _RESULTS_WORDS.search(text) else None  # other board meetings
            return kind
    return None


def extract_event_date(text: str, after: date) -> date | None:
    """The first date in ``text`` on or after ``after`` (a meeting or record date)."""
    found: list[date] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = m.groupdict()
            month = int(parts["mn"]) if parts.get("mn") else _MONTHS.get(parts["m"][:3].lower())
            if month is None:
                continue
            try:
                found.append(date(int(parts["y"]), month, int(parts["d"])))
            except ValueError:
                continue
    upcoming = sorted(d for d in found if d >= after)
    return upcoming[0] if upcoming else None


class DecisionCascade(Protocol):
    async def decide(
        self,
        task: str,
        state: Mapping[str, str],
        questions: Mapping[str, Question],
        *,
        decision_id: str | None = None,
        book_id: str | None = None,
    ) -> CascadeResult: ...


class EventClassifier:
    def __init__(
        self, *, cascade: Cascade | DecisionCascade, sink: EventSink, clock: Clock,
        context_tokens: int = 1024,
    ) -> None:  # fmt: skip
        self._cascade = cascade
        self._sink = sink
        self._clock = clock
        self._context = context_tokens

    async def classify(self, announcement: AnnouncementReceived) -> TypedEvent:
        state = announcement_state(announcement, self._context)
        result = await self._cascade.decide(TASK, state, QUESTIONS)
        answers = result.answers
        rule = subject_type(announcement.subject, announcement.title)
        kind = rule or _choice(answers.get("event_type"), AnnouncementType)
        relevant_answer = answers.get("relevant")
        if rule in (AnnouncementType.RESULTS, AnnouncementType.RESULTS_DATE):
            relevant = True  # results are always price-relevant
        elif relevant_answer is not None:
            relevant = bool(relevant_answer.value)
        else:
            relevant = True  # unknown: keep it visible to the gates
        published = announcement.published_at
        extra: dict[str, JsonValue] = {"subject": announcement.subject,
                                    "type_source": "exchange_subject" if rule else "model"}  # fmt: skip
        if kind is AnnouncementType.RESULTS_DATE:
            meeting = extract_event_date(announcement.title, published.astimezone(IST).date())
            if meeting is not None:
                extra["event_date"] = meeting.isoformat()
        elif kind is AnnouncementType.RESULTS:
            extra["event_date"] = published.astimezone(IST).date().isoformat()
        if result.abstained:
            extra["abstained"] = list(sorted(result.abstained))
        if result.escalated:
            extra["escalated"] = {k: result.escalated[k] for k in sorted(result.escalated)}
        models = sorted({a.model.split(":")[0] for a in answers.values() if a.model})
        event = TypedEvent(
            event_id=announcement.announcement_id, instrument_key=announcement.instrument_key,
            published_at=published, title=announcement.title, url=announcement.url,
            source=announcement.source, relevant=relevant, announcement_type=kind,
            direction=_choice(answers.get("direction"), EventDirection),
            materiality=_materiality(answers.get("materiality")),
            probabilities={q: {k: min(1.0, max(0.0, v)) for k, v in a.probabilities.items()}
                           for q, a in answers.items() if a.probabilities},
            model="+".join([*models, *(["rules"] if rule else [])]) or "none",
            calibrated=bool(answers) and all(a.calibrated for a in answers.values()),
            classified_at=self._clock.now(), extra=extra,
        )  # fmt: skip
        self._sink.emit(event, source="decision_models")
        return event


E = TypeVar("E", AnnouncementType, EventDirection)


def _choice(answer: Answer | None, enum: type[E]) -> E | None:
    if answer is None or not isinstance(answer.value, str):
        return None
    try:
        return enum(answer.value)
    except ValueError:
        return None


def _materiality(answer: Answer | None) -> Materiality | None:
    if answer is None:
        return None
    probs = {k: v for k, v in answer.probabilities.items() if k in MATERIALITY}
    if probs:
        return Materiality(max(probs, key=lambda k: probs[k]))
    if isinstance(answer.value, int | float) and not isinstance(answer.value, bool):
        index = min(len(MATERIALITY) - 1, max(0, round(float(answer.value))))
        return Materiality(MATERIALITY[index])
    return None


class Ingestor(Protocol):
    interval_s: float

    async def poll(self, *, force: bool = False) -> PollStats: ...


class AnnouncementPipeline:
    """The engine's ``announcements`` task: ingest new announcements, then classify each.

    Without a classifier (no decision model available) announcements are still stored; they are
    classified later from the ``backlog``. A classification failure is logged, never raised, and
    never holds back ingestion."""

    def __init__(
        self,
        *,
        ingestor: Ingestor,
        classifier: EventClassifier | None,
        backlog: Iterable[AnnouncementReceived] = (),
    ) -> None:
        self._ingestor = ingestor
        self._classifier = classifier
        self._backlog = list(backlog)

    @property
    def interval_s(self) -> float:
        return self._ingestor.interval_s

    async def poll(self, *, force: bool = False) -> list[TypedEvent]:
        stats = await self._ingestor.poll(force=force)
        pending, self._backlog = [*self._backlog, *stats.new], []
        return await self.classify_all(pending)

    async def classify_all(self, announcements: Iterable[AnnouncementReceived]) -> list[TypedEvent]:
        if self._classifier is None:
            return []
        out: list[TypedEvent] = []
        for announcement in announcements:
            try:
                out.append(await self._classifier.classify(announcement))
            except Exception:
                logger.exception("classifying %s failed", announcement.announcement_id)
        return out


def unclassified(
    received: Iterable[AnnouncementReceived], classified_ids: Iterable[str]
) -> list[AnnouncementReceived]:
    """Announcements stored but never classified (e.g. the process stopped in between)."""
    done = set(classified_ids)
    return [a for a in received if a.announcement_id not in done]
