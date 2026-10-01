"""Plan M1.5: the SQLite event store, migrations and projections."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.domain import events as ev
from src.domain.base import EventPayload
from src.domain.events import Event, make_event
from src.domain.types import (
    AnnouncementType,
    KillScope,
    KillSwitchState,
    OrderStatus,
    Side,
    TypedEvent,
)
from src.store.event_store import EventStore
from src.store.sqlite import SchemaTooNewError, connect, load_migrations, migrate, schema_version
from tests.factories import (
    INFY,
    T0,
    fill,
    order,
    position,
    risk_decision,
    sample_payloads,
)

PROJECTIONS = (
    "orders",
    "fills",
    "positions",
    "trades",
    "daily_risk_state",
    "kill_switches",
    "decisions",
    "llm_calls",
    "decision_model_calls",
    "typed_events",
)


def stamp(payload: EventPayload, minutes: int = 0) -> Event:
    return make_event(payload, ts=T0 + timedelta(minutes=minutes), source="test")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    with EventStore(tmp_path / "rq.db") as s:
        yield s


# --- schema --------------------------------------------------------------------------------


def test_fresh_store_is_migrated_wal_and_has_every_projection(tmp_path):
    with EventStore(tmp_path / "rq.db") as s:
        assert s.projection_tables == PROJECTIONS
        mode = s.query("SELECT * FROM pragma_journal_mode()")[0]
        assert next(iter(mode.values())) == "wal"
        tables = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"events", *PROJECTIONS} <= tables
    conn = connect(tmp_path / "rq.db")
    assert schema_version(conn) == len(load_migrations())
    assert migrate(conn) == len(load_migrations())  # idempotent
    conn.close()


def test_newer_schema_is_refused(tmp_path):
    conn = connect(tmp_path / "rq.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaTooNewError):
        EventStore(tmp_path / "rq.db")


def test_migration_files_must_be_numbered_contiguously(tmp_path):
    (tmp_path / "0001_init.sql").write_text("SELECT 1;")
    (tmp_path / "0003_late.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="without gaps"):
        load_migrations(tmp_path)
    (tmp_path / "0003_late.sql").unlink()
    (tmp_path / "2_bad.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="badly named"):
        load_migrations(tmp_path)


# --- round trip and reads ---------------------------------------------------------------------


def test_every_event_type_round_trips(store):
    payloads = sample_payloads()
    seqs = store.append_many([stamp(p, i) for i, p in enumerate(payloads)])
    assert seqs == list(range(1, len(payloads) + 1))
    assert store.last_seq() == len(payloads)
    back = store.read()
    assert [e.payload for e in back] == payloads
    first = back[0]
    assert first.seq == 1 and first.source == "test" and first.ts_utc == T0
    assert first.ts_utc.tzinfo is not None


def test_envelope_columns_survive(store):
    seq = store.append(
        make_event(ev.OrderSubmitted(order=order(book_id="B")), ts=T0, source="oms", cycle_id="cy7")
    )
    (e,) = store.read()
    assert (e.seq, e.decision_id, e.book_id, e.symbol, e.cycle_id) == (
        seq,
        "d-0001",
        "B",
        "INFY",
        "cy7",
    )
    assert str(e.ist_date) == "2026-10-05"


def test_read_filters(store):
    store.append_many(
        [
            stamp(ev.OrderSubmitted(order=order(book_id="A")), 0),
            stamp(ev.OrderSubmitted(order=order(book_id="B", client_order_id="c0002")), 1),
            stamp(ev.Heartbeat(pid=1, uptime_s=1), 2),
            make_event(ev.Heartbeat(pid=1, uptime_s=2), ts=T0 + timedelta(days=1), source="test"),
        ]
    )
    assert [e.seq for e in store.read(types=["OrderSubmitted"])] == [1, 2]
    assert [e.seq for e in store.read(book_id="B")] == [2]
    assert [e.seq for e in store.read(decision_id="d-0001")] == [1, 2]
    assert [e.seq for e in store.read(symbol="INFY", since_seq=1)] == [2]
    assert [e.seq for e in store.read(ist_date=date(2026, 10, 6))] == [4]
    assert [e.seq for e in store.read(limit=2)] == [1, 2]
    assert [e.seq for e in store.read(since_seq=3)] == [4]
    assert store.read(types=[]) == []


def test_stored_events_cannot_be_appended_again(store):
    store.append(stamp(ev.Heartbeat(pid=1, uptime_s=0)))
    (stored,) = store.read()
    with pytest.raises(ValueError, match="already stored"):
        store.append(stored)


def test_a_failing_batch_stores_nothing(store):
    store.append(stamp(ev.Heartbeat(pid=1, uptime_s=0)))
    good = stamp(ev.OrderSubmitted(order=order()))
    bad = stamp(ev.Heartbeat(pid=1, uptime_s=0)).with_seq(99)
    with pytest.raises(ValueError):
        store.append_many([good, bad])
    assert store.last_seq() == 1
    assert store.query("SELECT COUNT(*) AS n FROM orders")[0]["n"] == 0


def test_a_failing_projector_rolls_back_the_event(tmp_path):
    class Exploding:
        tables: tuple[str, ...] = ()
        handles: frozenset[str] = frozenset({"Heartbeat"})

        def apply(self, conn: sqlite3.Connection, event: Event) -> None:
            raise RuntimeError("projector bug")

    with EventStore(tmp_path / "rq.db") as s:
        s.register_projector(Exploding())
        with pytest.raises(RuntimeError, match="projector bug"):
            s.append(stamp(ev.Heartbeat(pid=1, uptime_s=0)))
        assert s.last_seq() == 0


def test_query_is_read_only(store):
    with pytest.raises(ValueError, match="SELECT"):
        store.query("DELETE FROM events")


def test_projector_tables_cannot_be_claimed_twice(store):
    from src.store.projections import OrdersProjector

    with pytest.raises(ValueError, match="already have a projector"):
        store.register_projector(OrdersProjector())


def test_events_and_projections_persist_across_reopen(tmp_path):
    path = tmp_path / "rq.db"
    with EventStore(path) as s:
        s.append(stamp(ev.OrderSubmitted(order=order())))
        before = s.snapshot_projections()
    with EventStore(path) as s:
        assert s.last_seq() == 1
        assert s.snapshot_projections() == before


# --- projections ----------------------------------------------------------------------------


def _fill_event(
    qty: int, filled: int, status: OrderStatus, fill_id: str = "f-0001"
) -> EventPayload:
    return ev.FillReceived(
        fill=fill(fill_id=fill_id, quantity=qty),
        order_status=status,
        order_filled_qty=filled,
        order_avg_price=Decimal("1513.10"),
    )


def test_order_lifecycle_projection(store):
    ref = {"client_order_id": "c0001", "book_id": "A", "decision_id": "d-0001"}
    store.append_many(
        [
            stamp(ev.OrderSubmitted(order=order(quantity=13))),
            stamp(
                ev.OrderAcked(
                    **ref, instrument_key=INFY.key, broker_order_id="SIM-1", status=OrderStatus.OPEN
                ),
                1,
            ),
            stamp(_fill_event(5, 5, OrderStatus.PARTIALLY_FILLED, "f-1"), 2),
            stamp(_fill_event(8, 13, OrderStatus.FILLED, "f-2"), 3),
        ]
    )
    (row,) = store.query("SELECT * FROM orders")
    assert row["status"] == "FILLED" and row["filled_qty"] == 13
    assert row["broker_order_id"] == "SIM-1" and row["avg_fill_price"] == "1513.10"
    assert (row["created_seq"], row["updated_seq"]) == (1, 4)
    assert {r["fill_id"] for r in store.query("SELECT fill_id FROM fills")} == {"f-1", "f-2"}


def test_duplicate_fills_are_counted_once(store):
    dup = _fill_event(13, 13, OrderStatus.FILLED)
    store.append_many([stamp(dup), stamp(dup, 1)])
    assert store.query("SELECT COUNT(*) AS n FROM fills")[0]["n"] == 1


def test_position_and_kill_switch_projections_keep_latest_state(store):
    store.append_many(
        [
            stamp(ev.PositionChanged(position=position(quantity=13)), 0),
            stamp(ev.PositionChanged(position=position(quantity=0, avg=None)), 1),
            stamp(
                ev.KillSwitchChanged(
                    book_id="A",
                    scope=KillScope.GLOBAL,
                    name="global",
                    previous=KillSwitchState.ARMED,
                    current=KillSwitchState.HALT_NEW,
                    reason="halt file",
                    actor="halt_file",
                ),
                2,
            ),
        ]
    )
    (pos,) = store.query("SELECT * FROM positions")
    assert pos["quantity"] == 0 and pos["avg_price"] is None and pos["updated_seq"] == 2
    (ks,) = store.query("SELECT * FROM kill_switches")
    assert (ks["state"], ks["actor"]) == ("HALT_NEW", "halt_file")


def test_every_projection_is_fed_by_the_sample_events(store):
    store.append_many([stamp(p, i) for i, p in enumerate(sample_payloads())])
    snapshot = store.snapshot_projections()
    assert all(snapshot[t] for t in PROJECTIONS), {t: len(r) for t, r in snapshot.items()}


# --- rebuild == incremental (property test, the M1.5 acceptance criterion) -------------------

_BOOKS = st.sampled_from(["A", "B"])
_ORDERS = st.sampled_from(["c1", "c2", "c3"])
_FILLS = st.sampled_from(["f1", "f2", "f3", "f4"])
_QTY = st.integers(min_value=1, max_value=40)


@st.composite
def _payloads(draw: st.DrawFn) -> EventPayload:
    kind = draw(st.integers(min_value=0, max_value=11))
    book, coid = draw(_BOOKS), draw(_ORDERS)
    ref = {
        "client_order_id": coid,
        "book_id": book,
        "decision_id": "d1",
        "instrument_key": INFY.key,
    }
    if kind == 0:
        return ev.OrderSubmitted(
            order=order(book_id=book, client_order_id=coid, quantity=draw(_QTY))
        )
    if kind == 1:
        return ev.OrderAcked(**ref, broker_order_id=f"SIM-{coid}", status=OrderStatus.OPEN)
    if kind == 2:
        return draw(
            st.sampled_from(
                [
                    ev.OrderRejected(**ref, reason="PRICE_BAND"),
                    ev.OrderUnknown(**ref, error="timeout"),
                    ev.OrderCancelled(**ref, filled_qty=1, reason="cap"),
                    ev.OrderExpired(**ref),
                ]
            )
        )
    if kind == 3:
        qty = draw(_QTY)
        return ev.FillReceived(
            fill=fill(book_id=book, fill_id=draw(_FILLS), client_order_id=coid, quantity=qty),
            order_status=OrderStatus.PARTIALLY_FILLED,
            order_filled_qty=qty,
            order_avg_price=Decimal("1500.05"),
        )
    if kind == 4:
        qty = draw(st.integers(min_value=-5, max_value=40))
        return ev.PositionChanged(
            position=position(book_id=book, quantity=qty, avg=None if qty == 0 else "1499.95")
        )
    if kind == 5:
        net = draw(st.integers(min_value=-500, max_value=500))
        return ev.TradeClosed(
            trade_id=draw(st.sampled_from(["t1", "t2"])),
            book_id=book,
            decision_id="d1",
            instrument_key=INFY.key,
            strategy="momentum",
            side=Side.BUY,
            quantity=draw(_QTY),
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            entry_ts=T0,
            exit_ts=T0,
            gross_pnl=Decimal(net),
            charges=Decimal("1.5"),
            net_pnl=Decimal(net) - Decimal("1.5"),
            exit_reason="target",
        )
    if kind == 6:
        return ev.KillSwitchChanged(
            book_id=book,
            scope=KillScope.STRATEGY,
            name=draw(st.sampled_from(["momentum", "mean_reversion"])),
            previous=KillSwitchState.ARMED,
            current=draw(st.sampled_from(list(KillSwitchState))),
            reason="test",
            actor="api",
        )
    if kind == 7:
        return risk_decision(book_id=book, intent_id=draw(st.sampled_from(["i1", "i2"])))
    if kind == 8:
        return ev.DailyRiskStateRolled(
            book_id=book, ist_date=date(2026, 10, 5), state={"entries": draw(_QTY)}
        )
    if kind == 9:
        return draw(
            st.sampled_from(
                [p for p in sample_payloads() if p.event_type in {"LLMCall", "DecisionModelCall"}]
            )
        )
    if kind == 10:
        return TypedEvent(
            event_id=draw(st.sampled_from(["e1", "e2"])),
            instrument_key=INFY.key,
            published_at=T0,
            title="Outcome of board meeting",
            source="nse_rss",
            relevant=draw(st.booleans()),
            announcement_type=AnnouncementType.RESULTS,
            model="laya",
            calibrated=False,
            classified_at=T0,
        )
    return ev.Heartbeat(pid=1, uptime_s=float(draw(_QTY)))


@contextmanager
def _scratch_store() -> Iterator[EventStore]:
    with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "rq.db") as s:
        yield s


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(payloads=st.lists(_payloads(), min_size=1, max_size=40), batch=st.integers(1, 7))
def test_rebuilt_projections_equal_incremental_ones(payloads, batch):
    events = [stamp(p, i) for i, p in enumerate(payloads)]
    with _scratch_store() as s:
        for start in range(0, len(events), batch):
            s.append_many(events[start : start + batch])
        incremental = s.snapshot_projections()
        assert s.rebuild_projections() == len(events)
        assert s.snapshot_projections() == incremental


def test_rebuild_repairs_damaged_projections(store):
    store.append_many([stamp(p, i) for i, p in enumerate(sample_payloads())])
    good = store.snapshot_projections()
    store._conn.execute("DELETE FROM orders")  # simulate a damaged read model
    store._conn.execute("UPDATE positions SET quantity = 999")
    assert store.snapshot_projections() != good
    store.rebuild_projections()
    assert store.snapshot_projections() == good
