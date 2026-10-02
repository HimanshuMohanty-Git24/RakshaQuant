"""Plan M7.8: the event-calendar gate - windows in trading sessions, point in time, entries only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from src.config.limits import load_risk_limits
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.types import (
    AnnouncementType,
    EventDirection,
    IntentKind,
    Materiality,
    Product,
    ReasonCode,
    RiskOutcome,
    Side,
    TypedEvent,
)
from src.engine.runner import Engine, EngineConfig, build_engine
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.risk.events import EventRules, event_blocks
from src.store.event_store import EventStore
from src.store.tape import read_bars, read_quotes
from src.utils.market_time import IST
from tests.factories import fill
from tests.oms_harness import INFY, harness
from tests.oms_harness import intent as oms_intent
from tests.test_engine_replay import DAY, TCS, record_tape
from tests.test_engine_replay import at as replay_at
from tests.test_risk_gate import entry, wire

R = ReasonCode
RULES = EventRules.from_yaml()
CAL = get_calendar()


def ist(d: date, hh: int = 10) -> datetime:
    return datetime.combine(d, time(hh), IST)


def typed(kind: AnnouncementType | None, published: datetime, *, event_date: str | None = None,
          direction: EventDirection | None = None, materiality: Materiality | None = None,
          relevant: bool = True, key: str = INFY.key, eid: str = "e1") -> TypedEvent:  # fmt: skip
    extra: dict[str, Any] = {"event_date": event_date} if event_date else {}
    return TypedEvent(event_id=eid, instrument_key=key, published_at=published, title="t",
                      source="nse_rss", relevant=relevant, announcement_type=kind,
                      direction=direction, materiality=materiality, model="laya+rules",
                      calibrated=False, classified_at=published, extra=extra)  # fmt: skip


def test_the_rules_file_loads_with_the_plan_defaults():
    assert RULES.results_before == RULES.results_after == 1 and RULES.adverse_after == 2
    assert RULES.results_types == {AnnouncementType.RESULTS, AnnouncementType.RESULTS_DATE}


def test_a_results_meeting_blocks_one_session_either_side_counting_trading_days():
    # Published Thu 1 Oct: the board meets Mon 5 Oct. Fri 2 Oct is a holiday, so the session
    # before is Thu 1 Oct and the one after is Tue 6 Oct.
    meeting = typed(AnnouncementType.RESULTS_DATE, ist(date(2026, 10, 1)), event_date="2026-10-05")
    (block,) = event_blocks([meeting], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 1), 12))[
        INFY.key
    ]
    assert (block.code, block.start, block.end) == (
        R.EVT_RESULTS_WINDOW,
        date(2026, 10, 1),
        date(2026, 10, 6),
    )
    assert block.covers(date(2026, 10, 5)) and not block.covers(date(2026, 10, 7))


def test_a_weekend_meeting_and_published_results():
    saturday = typed(AnnouncementType.RESULTS_DATE, ist(date(2026, 10, 7)), event_date="2026-10-10")
    (b,) = event_blocks([saturday], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 8)))[INFY.key]
    assert (b.start, b.end) == (date(2026, 10, 9), date(2026, 10, 12))  # Fri .. Mon
    out = typed(AnnouncementType.RESULTS, ist(date(2026, 10, 14), 16))  # results published Wed
    (b,) = event_blocks([out], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 14), 17))[INFY.key]
    assert (b.start, b.end) == (date(2026, 10, 13), date(2026, 10, 15))


def test_adverse_major_blocks_two_sessions_after_and_only_relevant_ones_count():
    bad = typed(AnnouncementType.LITIGATION_OR_REGULATORY, ist(date(2026, 10, 5)),
                direction=EventDirection.NEGATIVE, materiality=Materiality.MAJOR)  # fmt: skip
    (b,) = event_blocks([bad], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 5), 11))[INFY.key]
    assert (b.code, b.start, b.end) == (R.EVT_ADVERSE_MAJOR, date(2026, 10, 5), date(2026, 10, 7))
    ignored = [
        bad.model_copy(update={"relevant": False}),
        bad.model_copy(update={"materiality": Materiality.MODERATE}),
        bad.model_copy(update={"direction": None}),  # unknown direction: no veto by guess
    ]
    assert event_blocks(ignored, calendar=CAL, rules=RULES, now=ist(date(2026, 10, 5), 11)) == {}


def test_point_in_time_and_expiry():
    later = typed(AnnouncementType.RESULTS, ist(date(2026, 10, 5), 16))
    assert event_blocks([later], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 5), 10)) == {}
    old = typed(AnnouncementType.RESULTS, ist(date(2026, 9, 1)))
    assert event_blocks([old], calendar=CAL, rules=RULES, now=ist(date(2026, 10, 5))) == {}


def test_outside_calendar_coverage_weekdays_stand_in():
    late = typed(AnnouncementType.RESULTS_DATE, ist(date(2026, 12, 30)), event_date="2027-01-04")
    (b,) = event_blocks([late], calendar=CAL, rules=RULES, now=ist(date(2026, 12, 31)))[INFY.key]
    assert b.end == date(2027, 1, 5)


# --- through the RiskGate and the OMS ------------------------------------------------------------


async def test_acceptance_entries_are_blocked_in_a_results_window_exits_are_not(tmp_path):
    meeting = typed(AnnouncementType.RESULTS_DATE, datetime(2026, 10, 1, 4, tzinfo=UTC),
                    event_date="2026-10-05")  # fmt: skip
    with harness(tmp_path, gate=None) as h:  # Mon 5 Oct, 09:30 IST
        gate = wire(h)
        gate._events = lambda: [meeting]
        await h.oms.start()
        h.quote(1000.0)
        blocked = await h.oms.submit(entry())
        assert blocked.status == "BLOCKED" and "EVT_RESULTS_WINDOW" in blocked.message
        (decision,) = h.events("RiskDecision")
        assert decision.outcome is RiskOutcome.REJECTED
        assert decision.reasons[0].observed == "e1"

        gate._events = lambda: []  # the window does not apply to exits: hold, then sell
        assert (await h.oms.submit(entry(leg="e2"))).status == "SUBMITTED"
        h.quote(1001.0)
        gate._events = lambda: [meeting]
        exit_ = oms_intent(Side.SELL, 10, leg="exit", kind=IntentKind.CLOSE)
        assert (await h.oms.submit(exit_)).status == "SUBMITTED"


async def test_an_unavailable_event_calendar_blocks_entries_fail_closed(tmp_path):
    def broken() -> list[TypedEvent]:
        raise RuntimeError("store unreadable")

    with harness(tmp_path, gate=None) as h:
        gate = wire(h)
        gate._events = broken
        h.quote(1000.0)
        result = await h.oms.submit(entry())
        assert result.status == "BLOCKED" and "EVT_RESULTS_WINDOW" in result.message
        assert "unavailable" in h.events("RiskDecision")[0].reasons[0].message


@contextmanager
def offline_engine(tmp_path: Any) -> Iterator[Engine]:
    """A fully wired engine on the replay fixture tape, not running."""
    record_tape(tmp_path / "tape")
    with EventStore(tmp_path / "rq.db") as store:
        clock = ReplayClock(replay_at(10, 0))
        yield build_engine(
            config=EngineConfig(environment="paper"), clock=clock, calendar=CAL, store=store,
            quotes=TapeQuoteSource(read_quotes(tmp_path / "tape", DAY), clock=clock),
            history=TapeHistorySource(read_bars(tmp_path / "tape", DAY)),
            universe=[INFY, TCS], limits=load_risk_limits(),
        )  # fmt: skip


async def test_a_held_position_gets_an_alert_never_an_exit(tmp_path):
    with offline_engine(tmp_path) as engine:
        engine.oms.book.apply(fill(), product=Product.CNC, strategy="momentum")  # we hold INFY
        bad = typed(AnnouncementType.LITIGATION_OR_REGULATORY, engine.clock.now(),
                    direction=EventDirection.NEGATIVE, materiality=Materiality.MAJOR)  # fmt: skip
        other = bad.model_copy(update={"instrument_key": TCS.key, "event_id": "e2"})

        class Source:
            interval_s = 300.0

            async def poll(self, *, force: bool = False) -> list[TypedEvent]:
                return [bad, other]

        engine.announcements = Source()
        await engine.announcements_step()
        alerts = [e.payload for e in engine.store.read(types=["Alert"])]
        assert [a.key for a in alerts] == [f"held_adverse_event:{INFY.key}"]
        assert alerts[0].level == "CRITICAL" and engine.oms.orders == {}  # alert only, no exit


@pytest.mark.parametrize("bad", ["sessions_before: -1"])
def test_negative_session_counts_are_refused(tmp_path, bad):
    path = tmp_path / "rules.yaml"
    path.write_text(f"results_window:\n  types: [results]\n  {bad}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        EventRules.from_yaml(path)
