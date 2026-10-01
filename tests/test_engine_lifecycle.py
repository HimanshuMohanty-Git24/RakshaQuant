"""Plan M2.5: the clock-driven session lifecycle and its entry-window guards (frozen clock)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, time, timedelta

import pytest

from src.domain.calendar import DEFAULT_CALENDAR_PATH, NSECalendar, get_calendar
from src.domain.clock import ReplayClock
from src.domain.events import Alert, HolidaySkipped, SessionStateChanged
from src.domain.sink import RecordingSink
from src.domain.types import SessionState
from src.engine.lifecycle import (
    AIPurpose,
    LifecycleConfig,
    LifecycleHooks,
    Schedule,
    SessionLifecycle,
    build_schedule,
)
from src.ops.process import ConfigError
from src.utils.market_time import IST

S = SessionState
MONDAY = date(2026, 10, 5)


def ist(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), IST)


def make(
    start: datetime, *, calendar: NSECalendar | None = None, hooks: LifecycleHooks | None = None
):
    clock = ReplayClock(start)
    sink = RecordingSink(clock)
    lc = SessionLifecycle(clock=clock, calendar=calendar or get_calendar(), sink=sink,
                          hooks=hooks or LifecycleHooks())  # fmt: skip
    return lc, clock, sink


def timeline(sink: RecordingSink) -> list[tuple[SessionState, str]]:
    return [
        (e.payload.current, e.ts_utc.astimezone(IST).strftime("%H:%M"))
        for e in sink.events
        if isinstance(e.payload, SessionStateChanged)
    ]


async def drive(lc: SessionLifecycle, clock: ReplayClock, until: datetime) -> int:
    task = asyncio.create_task(lc.run())
    await clock.advance_to(until)
    return await asyncio.wait_for(task, timeout=2)


# --- acceptance: frozen-clock suite ---------------------------------------------------------


async def test_a_0900_start_waits_and_walks_the_whole_day():
    pre_open_at: list[str] = []

    async def on_pre_open(schedule: Schedule) -> None:
        pre_open_at.append(clock.now().astimezone(IST).strftime("%H:%M"))

    lc, clock, sink = make(ist(MONDAY, 9, 0), hooks=LifecycleHooks(on_pre_open=on_pre_open))
    assert await drive(lc, clock, ist(MONDAY, 16, 0)) == 0
    assert pre_open_at == ["09:00"]
    assert timeline(sink) == [
        (S.PRE_OPEN, "09:00"),
        (S.OPEN, "09:15"),
        (S.ENTRY_WINDOW, "09:20"),
        (S.MONITOR, "09:45"),
        (S.CLOSE, "15:30"),
        (S.REPORT, "15:45"),
        (S.EXIT, "15:50"),
    ]


async def test_a_holiday_exits_0_without_running_the_day():
    called: list[str] = []

    async def on_pre_open(schedule: Schedule) -> None:
        called.append("pre_open")

    lc, clock, sink = make(
        ist(date(2026, 10, 2), 9, 0), hooks=LifecycleHooks(on_pre_open=on_pre_open)
    )
    assert await lc.run() == 0
    assert timeline(sink) == [(S.HOLIDAY, "09:00")]
    (skipped,) = sink.payloads(HolidaySkipped)
    assert skipped.reason == "Mahatma Gandhi Jayanti" and called == []
    assert not lc.decisions_allowed()


async def test_saturday_is_blocked():
    lc, _, sink = make(ist(date(2026, 10, 3), 10, 0))
    assert await lc.run() == 0
    assert sink.payloads(HolidaySkipped)[0].reason == "weekend"
    assert not lc.decisions_allowed() and not lc.ai_call_allowed(AIPurpose.VETO)


@pytest.mark.parametrize(
    ("hh", "mm", "allowed"),
    [(9, 0, False), (9, 19, False), (9, 20, True), (9, 44, True), (9, 45, False), (10, 0, False),
     (15, 0, False)],
)  # fmt: skip
async def test_no_decisions_outside_the_entry_window(hh, mm, allowed):
    lc, clock, _ = make(ist(MONDAY, 8, 55))
    task = asyncio.create_task(lc.run())
    await clock.advance_to(ist(MONDAY, hh, mm))
    assert lc.decisions_allowed() is allowed
    assert lc.ai_call_allowed(AIPurpose.VETO) is allowed
    assert lc.ai_call_allowed(AIPurpose.EXPLAIN) is allowed
    assert lc.ai_call_allowed(AIPurpose.ANNOUNCEMENTS)  # the classifier runs all session
    assert lc.ai_call_allowed(AIPurpose.NIGHTLY_REVIEW)
    task.cancel()


# --- more cases -----------------------------------------------------------------------------------


async def test_late_start_runs_pre_open_then_jumps_to_now():
    ran: list[bool] = []

    async def on_pre_open(schedule: Schedule) -> None:
        ran.append(True)

    lc, clock, sink = make(ist(MONDAY, 11, 0), hooks=LifecycleHooks(on_pre_open=on_pre_open))
    assert await drive(lc, clock, ist(MONDAY, 16, 0)) == 0
    assert ran == [True]
    assert [s for s, _ in timeline(sink)] == [S.PRE_OPEN, S.MONITOR, S.CLOSE, S.REPORT, S.EXIT]
    assert timeline(sink)[1] == (S.MONITOR, "11:00")


async def test_start_after_the_day_ended_exits_immediately():
    lc, _, sink = make(ist(MONDAY, 18, 0))
    assert await lc.run() == 0
    assert timeline(sink) == [(S.EXIT, "18:00")]


async def test_on_state_sees_every_transition_and_failing_hooks_only_alert():
    seen: list[SessionState] = []

    async def on_state(state: SessionState, schedule: Schedule) -> None:
        seen.append(state)
        if state is S.REPORT:
            raise RuntimeError("report generator broke")

    lc, clock, sink = make(ist(MONDAY, 9, 0), hooks=LifecycleHooks(on_state=on_state))
    assert await drive(lc, clock, ist(MONDAY, 16, 0)) == 0
    assert seen == [S.OPEN, S.ENTRY_WINDOW, S.MONITOR, S.CLOSE, S.REPORT, S.EXIT]
    (alert,) = sink.payloads(Alert)
    assert alert.key == "lifecycle_hook_failed:on_state" and alert.level == "CRITICAL"


def _muhurat_calendar() -> NSECalendar:
    data = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    for item in data["years"]["2026"]["special_sessions"]:
        if item["date"] == "2026-11-08":
            item.update(pre_open="13:30", open="13:45", close="14:45")
    return NSECalendar(data)


async def test_a_session_without_room_for_the_entry_window_never_allows_decisions():
    day = date(2026, 11, 8)  # Muhurat (illustrative times)
    lc, clock, sink = make(ist(day, 13, 0), calendar=_muhurat_calendar())
    task = asyncio.create_task(lc.run())
    await clock.advance_to(ist(day, 14, 0))
    assert lc.state_now() is S.MONITOR and not lc.decisions_allowed()
    await clock.advance_to(ist(day, 15, 30))
    assert await asyncio.wait_for(task, 2) == 0
    assert [s for s, _ in timeline(sink)] == [S.PRE_OPEN, S.MONITOR, S.CLOSE, S.REPORT, S.EXIT]


async def test_a_date_outside_the_calendar_is_a_configuration_error():
    lc, _, _ = make(ist(date(2027, 1, 4), 9, 0))
    with pytest.raises(ConfigError, match="nse_calendar.json"):
        await lc.run()


def test_schedule_is_consistent_with_its_transitions():
    sched = build_schedule(get_calendar(), MONDAY, LifecycleConfig())
    assert sched is not None
    for state, at in sched.transitions():
        assert sched.state_at(at) is state
        assert sched.state_at(at - timedelta(seconds=1)) is not state


def test_config_is_validated():
    with pytest.raises(ValueError, match="entry window"):
        LifecycleConfig(entry_window_start=time(9, 45), entry_window_end=time(9, 20))
