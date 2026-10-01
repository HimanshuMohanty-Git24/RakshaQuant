"""
Durable key/value state for components that keep their own state outside the event log (the
simulated broker's exchange-side book, the exit manager's stops). Not a projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.store.event_store import EventStore


class StateStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, value: str, ts: datetime) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def load(self) -> str | None:
        return self.value

    def save(self, value: str, ts: datetime) -> None:
        self.value = value


class KVStateStore:
    """Keeps one component's state in the event store's ``kv_state`` table."""

    def __init__(self, store: EventStore, key: str, namespace: str) -> None:
        self._store, self._namespace, self._key = store, namespace, key

    def load(self) -> str | None:
        return self._store.kv_get(self._namespace, self._key)

    def save(self, value: str, ts: datetime) -> None:
        self._store.kv_put(self._namespace, self._key, value, ts)
