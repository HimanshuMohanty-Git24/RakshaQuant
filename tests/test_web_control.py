"""Plan M9.3: operator controls - cooperative stop, halt/resume/flatten, all audited as events."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.domain.clock import ReplayClock
from src.domain.types import KillScope, KillSwitchState, OrderStatus, Side
from src.engine.live import _drive
from src.web.run_manager import RunManager
from src.web.server import create_app
from tests.oms_harness import harness, intent
from tests.test_books import three_books
from tests.test_engine_live import RecordingView
from tests.test_engine_replay import at
from tests.web_helpers import ORIGIN, SECURITY, authed

RESUME = {"confirm": True, "phrase": "RESUME", "reason": "checked, resuming"}
FLATTEN = {"confirm": True, "phrase": "FLATTEN", "reason": "end the day flat"}


@pytest.fixture
def manager(settings, tmp_path):
    m = RunManager(settings, store_path=tmp_path / "web.db", clock=ReplayClock(at(10, 0)))
    yield m
    m.close_reader()


def client(manager):
    return authed(create_app(manager=manager, security=SECURITY))


def switches(manager) -> dict[str, str]:
    rows = manager.queries().store.query(
        "SELECT book_id, state FROM kill_switches WHERE scope = 'global'"
    )
    return {r["book_id"]: r["state"] for r in rows}


def commands(manager) -> list[Any]:
    return [e.payload for e in manager.queries().store.read(types=["ControlCommand"])]


# -- halt / resume / flatten ---------------------------------------------------------------------


def test_halt_resume_flatten_without_a_run(manager):
    c = client(manager)
    halted = c.post("/api/risk/halt", json={"reason": "manual check"}).json()
    assert halted["outcome"] == "applied" and halted["books"] == ["A", "B", "C"]
    assert switches(manager) == {"A": "HALT_NEW", "B": "HALT_NEW", "C": "HALT_NEW"}
    assert c.post("/api/risk/halt", json={"reason": "again"}).json()["outcome"] == "no_change"

    assert c.post("/api/risk/resume", json={**RESUME, "book": "B"}).json()["books"] == ["B"]
    assert switches(manager)["B"] == "ARMED" and switches(manager)["A"] == "HALT_NEW"
    flat = c.post("/api/risk/flatten", json={**FLATTEN, "book": "C"}).json()
    assert flat["outcome"] == "applied" and "next session" in flat["detail"]
    assert switches(manager)["C"] == "FLATTEN"

    audit = [(p.action, p.outcome, p.books) for p in commands(manager)]
    assert audit == [("halt", "applied", ("A", "B", "C")), ("halt", "no_change", ("A", "B", "C")),
                     ("resume", "applied", ("B",)), ("flatten", "applied", ("C",))]  # fmt: skip
    changes = manager.queries().store.read(types=["KillSwitchChanged"])
    assert {e.payload.actor for e in changes} == {"web"}
    assert c.post("/api/risk/halt", json={"reason": "x y z", "book": "Z"}).status_code == 404


def test_destructive_controls_need_confirm_and_the_typed_phrase(manager):
    c = client(manager)
    c.post("/api/risk/halt", json={"reason": "manual check"})
    for body in (
        {**RESUME, "confirm": "false"},  # a string is not a bool (acceptance)
        {**RESUME, "confirm": "true"},
        {**RESUME, "confirm": False},
        {**RESUME, "phrase": "resume"},
        {"confirm": True, "reason": "no phrase"},
        {**RESUME, "reason": ""},
        {**RESUME, "extra": 1},
    ):
        assert c.post("/api/risk/resume", json=body).status_code == 422, body
    assert c.post("/api/risk/flatten", json={**FLATTEN, "phrase": "RESUME"}).status_code == 422
    assert set(switches(manager).values()) == {"HALT_NEW"}  # nothing was re-armed


def test_a_halt_file_blocks_resume(manager):
    c = client(manager)
    c.post("/api/risk/halt", json={"reason": "manual check"})
    halt_file = manager.settings.halt_file
    halt_file.parent.mkdir(parents=True, exist_ok=True)
    halt_file.write_text("HALT", encoding="utf-8")
    res = c.post("/api/risk/resume", json=RESUME)
    assert res.status_code == 409 and res.json() == {"error": "a HALT file is present: remove "
                                                              "it first"}  # fmt: skip
    assert commands(manager)[-1].outcome == "refused"
    halt_file.unlink()
    assert c.post("/api/risk/resume", json=RESUME).json()["outcome"] == "applied"


def test_read_only_allows_halt_and_nothing_else(manager, monkeypatch):
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    c = client(manager)
    assert c.post("/api/risk/halt", json={"reason": "stop risk"}).status_code == 200
    assert c.post("/api/risk/resume", json=RESUME).status_code == 403
    assert c.post("/api/risk/flatten", json=FLATTEN).status_code == 403
    assert c.post("/api/session/start", json={"demo": False}).status_code == 403
    assert c.post("/api/session/stop").status_code == 403
    assert (
        c.post("/api/session/stop", headers={"Origin": "https://evil.example"}).status_code == 403
    )


async def test_with_a_running_engine_the_engines_own_switches_trip(settings, tmp_path):
    with three_books(tmp_path) as (engine, clock):
        m = RunManager(settings, clock=clock)
        m.attach(engine)
        try:
            client(m).post("/api/risk/halt", json={"reason": "news", "book": "A"})
            assert engine.books["A"].switches.state(KillScope.GLOBAL) is KillSwitchState.HALT_NEW
            assert engine.books["B"].switches.state(KillScope.GLOBAL) is KillSwitchState.ARMED
            assert engine.gate is engine.books["A"].gate  # the RiskGate reads this registry
            flat = client(m).post("/api/risk/flatten", json=FLATTEN).json()
            assert "next risk tick" in flat["detail"]
            assert [e.payload.action for e in engine.store.read(types=["ControlCommand"])] == [
                "halt", "flatten"]  # fmt: skip
        finally:
            m.close_reader()


# -- the session ---------------------------------------------------------------------------------


class Idle(RunManager):
    """A run that only waits for its stop event (no market data, no network)."""

    async def _run_real(self, view: Any) -> None:
        assert self._stop_event is not None
        await self._stop_event.wait()


def test_session_start_and_a_cooperative_stop(settings, tmp_path):
    m = Idle(settings, store_path=tmp_path / "web.db")
    with authed(create_app(manager=m, security=SECURITY)) as c:
        assert c.post("/api/session/stop").json()["outcome"] == "no_change"
        started = c.post("/api/session/start", json={"demo": False}, headers={"Origin": ORIGIN})
        assert started.json() == {"action": "session_start", "outcome": "applied", "books": [],
                                  "detail": "paper"}  # fmt: skip
        assert c.post("/api/session/start", json={"demo": False}).status_code == 409
        stopped = c.post("/api/session/stop").json()
        assert stopped["outcome"] == "applied" and not m.is_running


async def test_a_stop_ends_the_session_at_the_next_boundary(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        run = asyncio.create_task(engine.run())
        while engine.lifecycle.state is None or engine.lifecycle.state.value != "PRE_OPEN":
            await clock.advance(30)
        engine.request_stop()  # sleeping until OPEN: no further state is entered
        assert await asyncio.wait_for(run, timeout=5) == 0
        states = [e.payload.current.value for e in engine.store.read(types=["SessionStateChanged"])]
        assert states == ["PRE_OPEN"]
        assert any(e.payload.key == "session_stopped" for e in engine.store.read(types=["Alert"]))


async def test_a_stop_during_the_decision_cycle_lets_it_finish(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        original, release = engine.decision.run_cycle, asyncio.Event()
        entered = asyncio.Event()

        async def slow_cycle(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        engine.decision.run_cycle = slow_cycle  # type: ignore[method-assign]
        run = asyncio.create_task(engine.run())
        while not entered.is_set():
            await clock.advance(30)
        engine.request_stop()
        release.set()
        assert await asyncio.wait_for(run, timeout=10) == 0
        states = [e.payload.current.value for e in engine.store.read(types=["SessionStateChanged"])]
        assert states[-1] == "ENTRY_WINDOW"  # the cycle finished; nothing after it started
        assert engine.store.read(types=["OrderSubmitted"])  # its orders went out and are known
        assert all(o.client_order_id in engine.books[o.intent.book_id].oms.orders
                   for o in (e.payload.order for e in engine.store.read(types=["OrderSubmitted"])))  # fmt: skip


async def test_a_cancelled_submit_still_registers_its_order_and_fill(tmp_path):
    """Audit §N.1 #5: a stop issued mid-submit leaves no unregistered fill."""
    with harness(tmp_path) as h:
        await h.oms.start()
        release, placing = asyncio.Event(), asyncio.Event()
        place = h.broker.place_order

        async def slow_place(order: Any) -> Any:
            placing.set()
            await release.wait()
            return await place(order)

        h.quote(1000.0)  # the broker needs a market to accept the order
        h.broker.place_order = slow_place  # type: ignore[method-assign]
        caller = asyncio.create_task(h.oms.submit(intent(Side.BUY, 10)))
        await placing.wait()
        caller.cancel()  # the stop's hard cancel lands mid-submit
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await h.oms.stop()  # waits for the shielded submission to record its outcome
        await h.oms.start()
        (order,) = h.oms.orders.values()
        assert order.status is not OrderStatus.SUBMITTED and order.broker_order_id
        h.quote(1001.0)
        assert h.oms.orders[order.client_order_id].status is OrderStatus.FILLED
        assert (
            len(h.events("FillReceived")) == 1
            and h.book.quantity(order.intent.instrument.key, order.intent.product) == 10
        )


async def test_drive_cancels_an_engine_that_ignores_the_stop_after_the_grace(tmp_path):
    class Stubborn:
        stopped = False

        def request_stop(self) -> None:
            self.stopped = True

        async def run(self) -> int:
            await asyncio.Event().wait()  # never returns on its own
            return 0

    engine, stop = Stubborn(), asyncio.Event()
    stop.set()
    view = RecordingView()
    view.stats = None  # type: ignore[assignment]
    assert await asyncio.wait_for(_drive(engine, view, stop, grace_s=0.05), timeout=5) == 0  # type: ignore[arg-type]
    assert engine.stopped
