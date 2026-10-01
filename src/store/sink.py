"""An :class:`~src.domain.sink.EventSink` that appends to the event store."""

from __future__ import annotations

from src.domain.base import EventPayload
from src.domain.clock import Clock
from src.domain.events import Event, make_event
from src.store.event_store import EventStore


class StoreSink:
    def __init__(self, store: EventStore, clock: Clock, source: str) -> None:
        self._store = store
        self._clock = clock
        self._source = source

    def emit(
        self, payload: EventPayload, *, source: str | None = None, cycle_id: str | None = None
    ) -> Event:
        event = make_event(
            payload, ts=self._clock.now(), source=source or self._source, cycle_id=cycle_id
        )
        return event.with_seq(self._store.append(event))
