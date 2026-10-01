"""
Identifiers (plan M1.3).

* :func:`new_id` — time-ordered ids for decisions, cycles, fills, ...: 16 hex digits of
  nanoseconds since the epoch plus 8 random hex digits. Strictly increasing within a process,
  even when the clock returns the same reading twice.
* :func:`intent_id` — deterministic, so the same signal on the same bar always yields the same
  intent (and a restart can never place it twice). It includes the book: books A/B/C act on the
  same signal and must not share orders.
* :func:`client_order_id` — ``sha256(intent_id)[:16]``, sent to the broker as the order tag.
"""

import hashlib
import secrets
import threading
import time
from datetime import date

_lock = threading.Lock()
_last_ns = 0


def new_id() -> str:
    global _last_ns
    with _lock:
        now = max(time.time_ns(), _last_ns + 1)
        _last_ns = now
    return f"{now:016x}{secrets.token_hex(4)}"


def intent_id(
    *, book_id: str, strategy: str, instrument_key: str, signal_bar_date: date, leg: str
) -> str:
    """``"{book}:{strategy}:{instrument_key}:{bar date}:{leg}"``, e.g. ``A:momentum:NSE:EQ:INFY:2026-10-02:entry``."""
    for name, value in (("book_id", book_id), ("strategy", strategy), ("leg", leg)):
        if not value or ":" in value:
            raise ValueError(f"{name} must be non-empty and contain no ':' (got {value!r})")
    if not instrument_key:
        raise ValueError("instrument_key must be non-empty")
    return f"{book_id}:{strategy}:{instrument_key}:{signal_bar_date.isoformat()}:{leg}"


def client_order_id(intent: str) -> str:
    if not intent:
        raise ValueError("intent id must be non-empty")
    return hashlib.sha256(intent.encode("utf-8")).hexdigest()[:16]
