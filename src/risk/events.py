"""
The event calendar (plan M7.8): classified announcements (``TypedEvent``) → windows in which new
entries in an instrument are blocked, per the deterministic rules in
``src/config/event_rules.yaml``. Windows are counted in NSE trading sessions.

* ``EVT_RESULTS_WINDOW`` - from ``sessions_before`` sessions before to ``sessions_after``
  sessions after a results announcement, or a scheduled results board meeting (its
  ``extra.event_date``).
* ``EVT_ADVERSE_MAJOR`` - from the publication session through ``sessions_after`` sessions after
  a relevant negative + major event.

Only events **published** by the evaluation time count (point in time). These rules only block
new entries; exits never consult them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.types import (
    AnnouncementType,
    EventDirection,
    Materiality,
    ReasonCode,
    TypedEvent,
)
from src.utils.market_time import IST

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "event_rules.yaml"


@dataclass(frozen=True)
class EventRules:
    results_types: frozenset[AnnouncementType] = frozenset(
        {AnnouncementType.RESULTS, AnnouncementType.RESULTS_DATE}
    )
    results_before: int = 1
    results_after: int = 1
    adverse_direction: EventDirection = EventDirection.NEGATIVE
    adverse_materiality: Materiality = Materiality.MAJOR
    adverse_after: int = 2
    held_direction: EventDirection = EventDirection.NEGATIVE
    held_materiality: Materiality = Materiality.MAJOR

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_RULES_PATH) -> EventRules:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        results = data.get("results_window") or {}
        adverse = data.get("adverse_event") or {}
        held = data.get("held_alert") or {}
        rules = cls(
            results_types=frozenset(AnnouncementType(t) for t in results.get("types", [])),
            results_before=int(results.get("sessions_before", 1)),
            results_after=int(results.get("sessions_after", 1)),
            adverse_direction=EventDirection(adverse.get("direction", "negative")),
            adverse_materiality=Materiality(adverse.get("materiality", "major")),
            adverse_after=int(adverse.get("sessions_after", 2)),
            held_direction=EventDirection(held.get("direction", "negative")),
            held_materiality=Materiality(held.get("materiality", "major")),
        )
        if min(rules.results_before, rules.results_after, rules.adverse_after) < 0:
            raise ValueError("event rule session counts must be >= 0")
        return rules

    def is_adverse(self, event: TypedEvent) -> bool:
        return (
            event.relevant
            and event.direction is self.adverse_direction
            and event.materiality is self.adverse_materiality
        )

    def is_held_alert(self, event: TypedEvent) -> bool:
        return (
            event.relevant
            and event.direction is self.held_direction
            and event.materiality is self.held_materiality
        )


@dataclass(frozen=True)
class EventBlock:
    code: ReasonCode
    start: date  # first blocked session (inclusive)
    end: date  # last blocked session (inclusive)
    event_id: str
    reason: str

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


def event_blocks(
    events: Iterable[TypedEvent],
    *,
    calendar: NSECalendar,
    rules: EventRules,
    now: datetime,
) -> dict[str, tuple[EventBlock, ...]]:
    """Blocks per instrument key, from the events published by ``now``."""
    today = now.astimezone(IST).date()
    out: dict[str, list[EventBlock]] = defaultdict(list)
    for event in events:
        if event.published_at > now:
            continue  # not yet known at ``now``
        published = event.published_at.astimezone(IST).date()
        block: EventBlock | None = None
        if event.announcement_type in rules.results_types:
            day = _event_date(event) or published
            block = EventBlock(
                ReasonCode.EVT_RESULTS_WINDOW,
                _shift(calendar, day, -rules.results_before),
                _shift(calendar, day, rules.results_after),
                event.event_id,
                f"results {'meeting' if event.announcement_type is AnnouncementType.RESULTS_DATE else 'out'} "
                f"on {day.isoformat()}",
            )
        elif rules.is_adverse(event):
            block = EventBlock(
                ReasonCode.EVT_ADVERSE_MAJOR,
                published,
                _shift(calendar, published, rules.adverse_after),
                event.event_id,
                f"negative major event on {published.isoformat()}: {event.title[:80]}",
            )
        if block is not None and block.end >= today:
            out[event.instrument_key].append(block)
    return {k: tuple(v) for k, v in out.items()}


def _event_date(event: TypedEvent) -> date | None:
    value = event.extra.get("event_date")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _shift(calendar: NSECalendar, day: date, sessions: int) -> date:
    """The ``sessions``-th trading session before (negative) or after (positive) ``day``;
    0 = ``day`` itself. Outside the calendar's coverage, weekdays stand in for sessions."""
    current = day
    step = 1 if sessions > 0 else -1
    for _ in range(abs(sessions)):
        try:
            current = (
                calendar.next_trading_day(current) if step > 0
                else calendar.previous_trading_day(current)
            )  # fmt: skip
        except CalendarCoverageError:
            current += timedelta(days=step)
            while current.weekday() >= 5:
                current += timedelta(days=step)
    return current
