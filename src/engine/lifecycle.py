"""
Session lifecycle (plan M2.5), driven by the exchange calendar and a :class:`Clock`:

    HOLIDAY -> exit 0
    PRE_OPEN (refresh history + reference, reconcile) -> OPEN (09:15) -> ENTRY_WINDOW (09:20)
    -> MONITOR (09:45) -> CLOSE (15:30) -> REPORT (15:45) -> EXIT (15:50)

Every transition emits ``SessionStateChanged``. The guards (:meth:`SessionLifecycle.decisions_allowed`,
:meth:`SessionLifecycle.ai_call_allowed`) ask the *schedule* what state applies at the current
instant, so they stay right even if the lifecycle loop runs late: **no decisions outside the
entry window, and no LLM or decision-model calls outside it either**, except the announcement
classifier (M7) and the nightly review (M8).

* Started late, the lifecycle still runs pre-open, then jumps to the state for "now".
* The entry window is absolute IST (default 09:20–09:45). If it does not fit inside a session
  (e.g. a Muhurat evening session) that session has no entry window.
* CLOSE/REPORT/EXIT are relative to the session close (+0 / +15 / +20 min).
* A date outside the calendar's years is a configuration error (exit 2), never a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from src.domain.calendar import CalendarCoverageError, NSECalendar, Session
from src.domain.clock import Clock
from src.domain.events import Alert, HolidaySkipped, SessionStateChanged
from src.domain.sink import EventSink
from src.domain.types import SessionState
from src.ops.exit_codes import ExitCode
from src.ops.process import ConfigError
from src.utils.market_time import IST

logger = logging.getLogger(__name__)


class AIPurpose(StrEnum):
    """Why the engine wants an LLM / decision-model call (see :meth:`ai_call_allowed`)."""

    VETO = "veto"  # Book B/C advisors: entry window only
    EXPLAIN = "explain"
    ANNOUNCEMENTS = "announcements"  # M7 classifier: allowed all session
    NIGHTLY_REVIEW = "nightly_review"  # M8 review: allowed after the close


_ALWAYS_ALLOWED = frozenset({AIPurpose.ANNOUNCEMENTS, AIPurpose.NIGHTLY_REVIEW})


@dataclass(frozen=True)
class LifecycleConfig:
    entry_window_start: time = time(9, 20)
    entry_window_end: time = time(9, 45)
    report_after_close: timedelta = timedelta(minutes=15)
    exit_after_close: timedelta = timedelta(minutes=20)

    def __post_init__(self) -> None:
        if not self.entry_window_start < self.entry_window_end:
            raise ValueError("entry window must start before it ends")
        if not timedelta(0) <= self.report_after_close <= self.exit_after_close:
            raise ValueError("need 0 <= report_after_close <= exit_after_close")


@dataclass(frozen=True)
class Schedule:
    session: Session
    entry_start: datetime | None
    entry_end: datetime | None
    report: datetime
    exit: datetime

    @property
    def day(self) -> date:
        return self.session.day

    def state_at(self, ts: datetime) -> SessionState:
        t = ts.astimezone(IST)
        s = self.session
        if t < s.open:
            return SessionState.PRE_OPEN
        if t < s.close:
            if self.entry_start is None or self.entry_end is None:
                return SessionState.MONITOR
            if t < self.entry_start:
                return SessionState.OPEN
            if t < self.entry_end:
                return SessionState.ENTRY_WINDOW
            return SessionState.MONITOR
        if t < self.report:
            return SessionState.CLOSE
        if t < self.exit:
            return SessionState.REPORT
        return SessionState.EXIT

    def transitions(self) -> list[tuple[SessionState, datetime]]:
        """``(state, starts_at)`` after PRE_OPEN, in order."""
        s = self.session
        steps: list[tuple[SessionState, datetime]] = []
        if self.entry_start is None or self.entry_end is None:
            steps.append((SessionState.MONITOR, s.open))
        else:
            if self.entry_start > s.open:
                steps.append((SessionState.OPEN, s.open))
            steps.append((SessionState.ENTRY_WINDOW, self.entry_start))
            if self.entry_end < s.close:
                steps.append((SessionState.MONITOR, self.entry_end))
        steps += [
            (SessionState.CLOSE, self.session.close),
            (SessionState.REPORT, self.report),
            (SessionState.EXIT, self.exit),
        ]
        return steps


def build_schedule(calendar: NSECalendar, day: date, config: LifecycleConfig) -> Schedule | None:
    """The day's schedule, or None when the market does not trade that day."""
    session = calendar.session(day)
    if session is None:
        return None
    start = datetime.combine(day, config.entry_window_start, IST)
    end = datetime.combine(day, config.entry_window_end, IST)
    fits = session.open <= start and end <= session.close
    return Schedule(
        session=session,
        entry_start=start if fits else None,
        entry_end=end if fits else None,
        report=session.close + config.report_after_close,
        exit=session.close + config.exit_after_close,
    )


@dataclass
class LifecycleHooks:
    """Async callbacks; a failing hook is logged and alerted, and the lifecycle carries on."""

    on_pre_open: Callable[[Schedule], Awaitable[None]] | None = None
    on_state: Callable[[SessionState, Schedule], Awaitable[None]] | None = None


@dataclass
class SessionLifecycle:
    clock: Clock
    calendar: NSECalendar
    sink: EventSink
    config: LifecycleConfig = field(default_factory=LifecycleConfig)
    hooks: LifecycleHooks = field(default_factory=LifecycleHooks)
    state: SessionState | None = field(default=None, init=False)
    schedule: Schedule | None = field(default=None, init=False)

    # -- guards ----------------------------------------------------------------------------------

    def state_now(self) -> SessionState | None:
        if self.schedule is None:
            return self.state  # HOLIDAY, or not started
        return self.schedule.state_at(self.clock.now())

    def decisions_allowed(self) -> bool:
        """Entry decisions happen only inside the entry window."""
        return self.state_now() is SessionState.ENTRY_WINDOW

    def ai_call_allowed(self, purpose: AIPurpose) -> bool:
        if purpose in _ALWAYS_ALLOWED:
            return True
        return self.decisions_allowed()

    # -- driving ---------------------------------------------------------------------------------

    async def run(self) -> int:
        """Drive the day to EXIT (or HOLIDAY); return the process exit code."""
        today = self.clock.now().astimezone(IST).date()
        try:
            self.schedule = build_schedule(self.calendar, today, self.config)
        except CalendarCoverageError as exc:
            raise ConfigError(str(exc)) from exc

        if self.schedule is None:
            reason = self._closed_reason(today)
            self.sink.emit(HolidaySkipped(session_date=today, reason=reason), source="lifecycle")
            self._transition(SessionState.HOLIDAY, today)
            logger.info("No session on %s (%s); exiting", today, reason)
            return ExitCode.OK

        schedule = self.schedule
        if self.clock.now() >= schedule.exit:
            self._transition(SessionState.EXIT, today)
            return ExitCode.OK

        self._transition(SessionState.PRE_OPEN, today)
        await self._hook("on_pre_open", self.hooks.on_pre_open, schedule)

        current = schedule.state_at(self.clock.now())
        if current is not SessionState.PRE_OPEN:
            await self._enter(current, schedule)  # late start: skip what already passed
        for state, at in schedule.transitions():
            if at <= self.clock.now():
                continue
            await self.clock.sleep_until(at)
            await self._enter(state, schedule)
        return ExitCode.OK

    async def _enter(self, state: SessionState, schedule: Schedule) -> None:
        self._transition(state, schedule.day)
        on_state = self.hooks.on_state
        if on_state is not None:
            await self._hook("on_state", lambda s: on_state(state, s), schedule)

    def _transition(self, state: SessionState, day: date) -> None:
        if state is self.state:
            return
        previous, self.state = self.state, state
        self.sink.emit(
            SessionStateChanged(session_date=day, previous=previous, current=state),
            source="lifecycle",
        )
        logger.info("Session %s: %s -> %s", day, previous, state)

    async def _hook(
        self,
        name: str,
        hook: Callable[[Schedule], Awaitable[None]] | None,
        schedule: Schedule,
    ) -> None:
        if hook is None:
            return
        try:
            await hook(schedule)
        except Exception as exc:
            logger.exception("lifecycle hook %s failed", name)
            self.sink.emit(
                Alert(
                    level="CRITICAL",
                    key=f"lifecycle_hook_failed:{name}",
                    message=f"{name} failed in {self.state}: {type(exc).__name__}: {exc}",
                ),
                source="lifecycle",
            )

    def _closed_reason(self, day: date) -> str:
        holiday = self.calendar.holiday_name(day)
        if holiday:
            return holiday
        if day.weekday() >= 5:
            return "weekend"
        return "no session scheduled"
