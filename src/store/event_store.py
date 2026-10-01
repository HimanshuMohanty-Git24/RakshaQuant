"""
The event store (plan M1.5; audit §G.4): an append-only ``events`` table in SQLite (WAL) plus
projections kept in step **in the same transaction**. Events are the system of record;
projections are disposable and can be rebuilt from them at any time.

    with EventStore(settings.db_path) as store:
        seq = store.append(make_event(payload, ts=clock.now(), source="oms"))
        lineage = store.read(decision_id="...")
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from src.domain.events import Event, load_payload, payload_json
from src.store.projections import DEFAULT_PROJECTORS, Projector
from src.store.sqlite import connect, migrate

_FETCH_BATCH = 1000


class EventStore:
    def __init__(self, path: Path, projectors: Sequence[Projector] = DEFAULT_PROJECTORS) -> None:
        self.path = path
        self._conn = connect(path)
        self._lock = threading.RLock()
        migrate(self._conn)
        self._projectors: list[Projector] = []
        for projector in projectors:
            self.register_projector(projector)

    # -- lifecycle ---------------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def register_projector(self, projector: Projector) -> None:
        """Add a projector. If events already exist, call :meth:`rebuild_projections` after."""
        owned = {t for p in self._projectors for t in p.tables}
        clash = owned.intersection(projector.tables)
        if clash:
            raise ValueError(f"tables {sorted(clash)} already have a projector")
        self._projectors.append(projector)

    @property
    def projection_tables(self) -> tuple[str, ...]:
        return tuple(t for p in self._projectors for t in p.tables)

    # -- writing -----------------------------------------------------------------------------

    def append(self, event: Event) -> int:
        """Append one event (and update projections) atomically; return its ``seq``."""
        return self.append_many([event])[0]

    def append_many(self, events: Sequence[Event]) -> list[int]:
        """Append events in one transaction: either all are stored and projected, or none."""
        if not events:
            return []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                seqs = [self._insert(event) for event in events]
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return seqs

    def _insert(self, event: Event) -> int:
        if event.seq is not None:
            raise ValueError(f"event already stored (seq={event.seq})")
        cursor = self._conn.execute(
            "INSERT INTO events (ts_utc, ist_date, type, schema_version, decision_id, cycle_id,"
            " book_id, symbol, source, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event.ts_utc.isoformat(),
                event.ist_date.isoformat(),
                event.type,
                event.schema_version,
                event.decision_id,
                event.cycle_id,
                event.book_id,
                event.symbol,
                event.source,
                payload_json(event.payload),
            ),
        )
        seq = cursor.lastrowid
        if seq is None:  # pragma: no cover - sqlite always reports the rowid of an INSERT
            raise RuntimeError("sqlite did not return a seq")
        self._project(event.with_seq(seq))
        return seq

    def _project(self, stored: Event) -> None:
        for projector in self._projectors:
            if stored.type in projector.handles:
                projector.apply(self._conn, stored)

    def rebuild_projections(self) -> int:
        """Empty every projection and replay all events through the projectors; return count."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in self.projection_tables:
                    self._conn.execute(f"DELETE FROM {table}")  # names come from projectors
                count = 0
                for event in self._iter_events("", ()):
                    self._project(event)
                    count += 1
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return count

    # -- reading -----------------------------------------------------------------------------

    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def read(
        self,
        *,
        since_seq: int = 0,
        types: Iterable[str] | None = None,
        decision_id: str | None = None,
        symbol: str | None = None,
        book_id: str | None = None,
        ist_date: date | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Events with ``seq > since_seq`` matching every given filter, oldest first."""
        clauses = ["seq > ?"]
        params: list[Any] = [since_seq]
        if types is not None:
            wanted = list(types)
            if not wanted:
                return []
            clauses.append(f"type IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        for column, value in (
            ("decision_id", decision_id),
            ("symbol", symbol),
            ("book_id", book_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if ist_date is not None:
            clauses.append("ist_date = ?")
            params.append(ist_date.isoformat())
        where = " WHERE " + " AND ".join(clauses)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            where += " ORDER BY seq LIMIT ?"
            params.append(limit)
            with self._lock:
                rows = self._conn.execute(f"SELECT * FROM events{where}", params).fetchall()
            return [_row_to_event(r) for r in rows]
        with self._lock:
            return list(self._iter_events(where, params))

    def _iter_events(self, where: str, params: Sequence[Any]) -> Iterator[Event]:
        cursor = self._conn.execute(f"SELECT * FROM events{where} ORDER BY seq", params)
        while batch := cursor.fetchmany(_FETCH_BATCH):
            for row in batch:
                yield _row_to_event(row)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a read-only ``SELECT`` (e.g. against a projection) and return dict rows."""
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise ValueError("query() only runs SELECT statements; write through append()")
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def snapshot_projections(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every projection table's rows in a canonical order (for comparisons and golden tests)."""
        snapshot: dict[str, list[tuple[Any, ...]]] = {}
        with self._lock:
            for table in self.projection_tables:
                rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
                snapshot[table] = sorted((tuple(row) for row in rows), key=_null_safe_key)
        return snapshot


def _null_safe_key(row: tuple[Any, ...]) -> tuple[tuple[int, Any], ...]:
    return tuple((0, 0) if value is None else (1, value) for value in row)


def _row_to_event(row: Any) -> Event:
    return Event(
        type=row["type"],
        payload=load_payload(row["type"], row["schema_version"], row["payload"]),
        ts_utc=datetime.fromisoformat(row["ts_utc"]),
        ist_date=date.fromisoformat(row["ist_date"]),
        source=row["source"],
        schema_version=row["schema_version"],
        seq=row["seq"],
        decision_id=row["decision_id"],
        cycle_id=row["cycle_id"],
        book_id=row["book_id"],
        symbol=row["symbol"],
    )
