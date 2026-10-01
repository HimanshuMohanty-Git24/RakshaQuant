"""
Event sinks: how producers (market data, lifecycle, OMS, ...) emit events without knowing where
they go. A sink stamps the payload with its clock's time and records it.

* :class:`RecordingSink` keeps events in memory (tests, demo).
* ``src.store.sink.StoreSink`` appends them to the event store.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from src.domain.base import EventPayload
from src.domain.clock import Clock
from src.domain.events import Event, make_event

P = TypeVar("P", bound=EventPayload)


class EventSink(Protocol):
    def emit(
        self, payload: EventPayload, *, source: str | None = None, cycle_id: str | None = None
    ) -> Event:
        """Record ``payload`` now; return the stored event (with ``seq``)."""
        ...


class RecordingSink:
    def __init__(self, clock: Clock, source: str = "test") -> None:
        self._clock = clock
        self._source = source
        self.events: list[Event] = []

    def emit(
        self, payload: EventPayload, *, source: str | None = None, cycle_id: str | None = None
    ) -> Event:
        event = make_event(
            payload, ts=self._clock.now(), source=source or self._source, cycle_id=cycle_id
        ).with_seq(len(self.events) + 1)
        self.events.append(event)
        return event

    def payloads(self, kind: type[P]) -> list[P]:
        return [e.payload for e in self.events if isinstance(e.payload, kind)]
