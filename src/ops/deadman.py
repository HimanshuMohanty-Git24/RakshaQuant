"""
The dead-man check (plan M12.4). Task Scheduler runs it at 10:00 and 13:00 IST on weekdays
(``scripts/deadman_check.py``). During market hours, it raises the alarm when the engine's last
``Heartbeat`` (every 30 s) is older than :data:`STALE_AFTER_S`, or when there is none today.

It only reads: the engine may be running, and WAL readers never block it, so the check takes
no lock and never writes to the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.events import Heartbeat
from src.store.event_store import EventStore
from src.utils.market_time import IST

STALE_AFTER_S = 180.0

Status = Literal["ok", "stale", "skipped"]


@dataclass(frozen=True)
class DeadmanResult:
    status: Status
    detail: str
    last_beat: datetime | None = None

    @property
    def alarm(self) -> bool:
        return self.status == "stale"


def check(
    store_path: Path,
    *,
    now: datetime,
    calendar: NSECalendar,
    stale_after_s: float = STALE_AFTER_S,
) -> DeadmanResult:
    """Is the engine alive? ``skipped`` outside market hours (holidays and weekends too)."""
    local = now.astimezone(IST)
    try:
        open_now = calendar.is_market_open(now)
    except CalendarCoverageError:
        return DeadmanResult("stale", f"the NSE calendar does not cover {local.date()}: the "
                                      "engine refuses to run until it does")  # fmt: skip
    if not open_now:
        return DeadmanResult("skipped", f"the market is closed at {local:%Y-%m-%d %H:%M} IST")
    if not store_path.exists():
        return DeadmanResult("stale", f"no event store at {store_path.name}: no session has run")
    with EventStore(store_path) as store:
        beats = store.read(types=[Heartbeat.event_type], ist_date=local.date())
    if not beats:
        return DeadmanResult("stale", f"no heartbeat today ({local.date()}): the session is not "
                                      "running")  # fmt: skip
    last = beats[-1].ts_utc
    age = (now - last).total_seconds()
    when = f"{last.astimezone(IST):%H:%M:%S} IST"
    if age > stale_after_s:
        return DeadmanResult("stale", f"no heartbeat for {age / 60:.0f} min (last at {when})",
                             last)  # fmt: skip
    return DeadmanResult("ok", f"last heartbeat {age:.0f} s ago ({when})", last)


def alarm_text(result: DeadmanResult, environment: str) -> str:
    return (f"RakshaQuant dead-man ({environment}): {result.detail}. Check the session's "
            "console window and var/logs/; see docs/runbooks/incident.md.")  # fmt: skip
