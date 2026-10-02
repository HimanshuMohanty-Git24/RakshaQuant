"""Plan M9.2: the REST projections, over a real three-book session (tests/test_books.py)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from src.config.limits import load_risk_limits
from src.domain.events import MarkToMarket
from src.web.run_manager import RunManager
from src.web.server import create_app
from tests.test_books import run as run_day
from tests.test_books import three_books
from tests.test_engine_replay import DAY, INFY, TCS, at
from tests.web_helpers import SECURITY, anonymous, authed

GETS = ["/api/summary", "/api/positions", "/api/orders", "/api/fills", "/api/trades",
        "/api/decisions", "/api/risk", "/api/books", "/api/ai/calls", "/api/ai/spend",
        "/api/ai/models", "/api/ai/decision-models", "/api/market/INFY/bars",
        "/api/events/typed", "/api/system", "/api/config", "/api/state"]  # fmt: skip


@pytest.fixture
async def session(settings, tmp_path):
    """A recorded day (A: no advisor, B vetoes TCS, C abstains) and a manager over its store."""
    with three_books(tmp_path) as (engine, clock):
        assert await run_day(engine, clock) == 0
        local = settings.model_copy(update={"var_dir": tmp_path})  # tape at tmp_path/tape
        manager = RunManager(local, store_path=engine.store.path, clock=clock)
        yield engine, manager
        manager.close_reader()


def client_for(manager):
    return authed(create_app(manager=manager, security=SECURITY))


def last_marks(engine) -> dict[str, MarkToMarket]:
    return {e.book_id: e.payload for e in engine.store.read(types=["MarkToMarket"])}


@pytest.mark.parametrize("path", GETS)
def test_every_endpoint_needs_the_token(path):
    assert anonymous(create_app(security=SECURITY)).get(path).status_code == 401


async def test_summary_from_the_last_marks_and_live_from_the_engine(session):
    engine, manager = session
    client = client_for(manager)
    summary = client.get("/api/summary").json()
    marks = last_marks(engine)
    assert summary["mode"] == "paper" and summary["running"] is False
    assert summary["session"] == {"date": DAY.isoformat(), "state": "EXIT"}
    assert summary["last_seq"] == engine.store.last_seq()
    books = {b["book_id"]: b for b in summary["books"]}
    assert set(books) == {"A", "B", "C"} and books["B"]["advisor"] == "typed_veto"
    for book_id, b in books.items():
        assert b["valuation"] == "last_mark" and Decimal(b["equity"]) == marks[book_id].equity
        assert b["kill_switch"] == "ARMED" and b["open_orders"] == 0
    closed = engine.store.query("SELECT book_id, net_pnl FROM trades")
    assert books["A"]["trades_today"] == sum(1 for r in closed if r["book_id"] == "A") == 2
    assert Decimal(books["A"]["realized_pnl_today"]) == sum(
        Decimal(r["net_pnl"]) for r in closed if r["book_id"] == "A"
    )

    manager.attach(engine)  # a running engine: valued on its live marks
    live = {b["book_id"]: b for b in client_for(manager).get("/api/summary").json()["books"]}
    assert live["A"]["valuation"] == "live"
    assert Decimal(live["A"]["equity"]) == engine.valuation("A").equity


async def test_the_blotter_filters(session):
    engine, manager = session
    client = client_for(manager)
    orders = client.get("/api/orders", params={"book": "B"}).json()
    assert orders and {o["book_id"] for o in orders} == {"B"}
    assert all(o["ist_date"] == DAY.isoformat() for o in orders)
    filled = client.get("/api/orders", params={"book": "B", "status": "FILLED"}).json()
    assert filled and all(o["status"] == "FILLED" for o in filled)
    assert len(client.get("/api/orders", params={"limit": 1}).json()) == 1
    assert client.get("/api/orders", params={"date": "2026-10-06"}).json() == []
    fills = client.get("/api/fills", params={"book": "A", "date": DAY.isoformat()}).json()
    assert len(fills) == 4 and all(Decimal(f["charges"]) > 0 for f in fills)  # 2 round trips
    trades = client.get("/api/trades", params={"book": "A"}).json()
    assert {t["symbol"]: t["exit_reason"] for t in trades} == {"INFY": "target", "TCS": "stop"}
    assert client.get("/api/trades", params={"strategy": "breakout"}).json() == []
    assert client.get("/api/positions").json() == []  # everything closed on the day
    for bad in ({"book": "B; DROP"}, {"limit": 0}, {"limit": 5000}, {"date": "05-10-2026"}):
        assert client.get("/api/orders", params=bad).status_code == 422


async def test_decisions_and_a_full_lineage(session):
    engine, manager = session
    client = client_for(manager)
    vetoed = client.get("/api/decisions", params={"book": "B", "outcome": "vetoed"}).json()
    assert [d["symbol"] for d in vetoed] == ["TCS"]
    infy_all = client.get("/api/decisions", params={"symbol": "INFY", "book": "A"}).json()
    assert {d["disposition"] for d in infy_all} == {"submitted", "shadow_strategy"}
    infy = client.get("/api/decisions", params={"symbol": "INFY", "book": "A",
                                                "outcome": "submitted"}).json()  # fmt: skip
    assert [d["strategy"] for d in infy] == ["momentum"]
    assert client.get("/api/decisions", params={"outcome": "nonsense"}).status_code == 422

    lineage = client.get(f"/api/decisions/{infy[0]['decision_id']}").json()
    types = [e["type"] for e in lineage["events"]]
    for expected in ("SignalGenerated", "OrderIntentProposed", "RiskDecision", "OrderSubmitted",
                     "FillReceived", "TradeClosed"):  # fmt: skip
        assert expected in types
    seqs = [e["seq"] for e in lineage["events"]]
    assert seqs == sorted(seqs) and {e["book_id"] for e in lineage["events"]} >= {"A", "B", "C"}
    assert lineage["exit_decision_ids"]  # the target exit's own decision is included
    exit_events = [e for e in lineage["events"] if e["decision_id"] in lineage["exit_decision_ids"]]
    assert any(e["type"] == "FillReceived" for e in exit_events)
    assert client.get("/api/decisions/does-not-exist").status_code == 404
    assert client.get("/api/decisions/bad%20id").status_code == 422


async def test_risk_and_the_paired_books(session):
    engine, manager = session
    client = client_for(manager)
    risk = client.get("/api/risk").json()
    assert risk["limits_hash"] == load_risk_limits().limits_hash()
    assert [b["book_id"] for b in risk["books"]] == ["A", "B", "C"]
    assert client.get("/api/risk", params={"book": "B"}).json()["books"][0]["book_id"] == "B"

    books = {b["book_id"]: b for b in client.get("/api/books").json()["books"]}
    tcs_loss = next(
        Decimal(r["net_pnl"])
        for r in engine.store.query("SELECT instrument_key, net_pnl FROM trades WHERE book_id='A'")
        if r["instrument_key"] == TCS.key
    )
    assert books["A"]["vs"] is None and books["B"]["vs"] == "A"
    assert Decimal(books["B"]["equity_difference_inr"]) == -tcs_loss  # B skipped A's loser
    assert Decimal(books["B"]["net_ai_value_inr"]) == -tcs_loss - Decimal(
        books["B"]["ai_spend_inr_to_date"]
    )
    assert Decimal(books["C"]["equity_difference_inr"]) == 0
    precision = books["B"]["veto_precision"]
    assert (precision["vetoes"], precision["settled"], precision["correct"]) == (1, 1, 1)


async def test_ai_market_events_reports_system_config(session, settings):
    engine, manager = session
    client = client_for(manager)
    assert client.get("/api/ai/calls").json() == []
    spend = client.get("/api/ai/spend", params={"group_by": "book"}).json()
    assert spend["group_by"] == "book" and Decimal(spend["total_inr"]) == 0
    assert client.get("/api/ai/spend", params={"group_by": "everything"}).status_code == 422
    roles = {r["role"]: r for r in client.get("/api/ai/models").json()}
    assert "veto" in roles and roles["veto"]["enabled"] is False
    assert client.get("/api/ai/decision-models").json() == []

    taped = client.get("/api/market/INFY/bars", params={"days": 30}).json()
    assert taped["source"] == "tape" and taped["instrument_key"] == INFY.key
    assert len(taped["bars"]) == 30
    manager.attach(engine)
    live = client_for(manager).get("/api/market/INFY/bars").json()
    assert live["source"] == "engine" and live["bars"][-1]["date"] < DAY.isoformat()
    assert client.get("/api/market/UNKNOWN/bars").json()["source"] == "none"
    assert client.get("/api/market/bad%20sym/bars").status_code == 422
    assert client.get("/api/events/typed").json() == []

    reports = manager.settings.reports_dir
    reports.mkdir(parents=True)
    (reports / f"{DAY}.json").write_text(json.dumps({"date": DAY.isoformat()}), encoding="utf-8")
    assert client_for(manager).get(f"/api/reports/{DAY}").json() == {"date": DAY.isoformat()}
    assert client.get("/api/reports/2026-10-06").status_code == 404
    assert client.get("/api/reports/..%2F..%2Fsecrets").status_code in (404, 422)

    system = client_for(manager).get("/api/system").json()
    assert system["store_path"] == "rq.db" and system["last_seq"] == engine.store.last_seq()
    assert system["schema_version"] >= 2 and system["store_bytes"] > 0

    response = client.get("/api/config")
    config = response.json()
    assert config["mode"] == "paper" and config["experiment"]["learning_injection"] is False
    assert "test-groq-key" not in response.text  # never a secret
    assert set(config["llm_roles"]) >= {"veto", "review"}


# -- plan M10.4: what the screens need ------------------------------------------------------------


async def test_mid_session_positions_carry_their_exits_and_risk_its_utilisation(settings, tmp_path):
    """Stopped at 10:30 with INFY (and, in A, TCS) open: stops, targets, heat, sectors."""
    with three_books(tmp_path) as (engine, clock):
        run = asyncio.create_task(engine.run())
        while clock.now() < at(10, 30):
            await clock.advance(30)
        manager = RunManager(settings, clock=clock)
        manager.attach(engine)
        try:
            client = client_for(manager)
            positions = client.get("/api/positions", params={"book": "A"}).json()
            infy = next(p for p in positions if p["symbol"] == "INFY")
            assert infy["strategy"] == "momentum" and infy["entered_on"] == DAY.isoformat()
            assert (
                Decimal(infy["stop_price"])
                < Decimal(infy["avg_price"])
                < Decimal(infy["target_price"])
            )
            assert infy["held_sessions"] == 0 and infy["entry_decision_id"]
            assert (
                Decimal(infy["unrealized_pnl"])
                == (Decimal(infy["mark"]) - Decimal(infy["avg_price"])) * infy["quantity"]
            )
            risk = client.get("/api/risk", params={"book": "A"}).json()["books"][0]
            usage = {u["key"]: u for u in risk["utilisation"]}
            assert risk["valuation"] == "live" and Decimal(usage["PF_HEAT"]["used"]) > 0
            assert usage["PF_MAX_POSITIONS"]["used"] == str(len(positions))
            assert 0 < usage["PF_GROSS"]["fraction"] < 1
            assert risk["sectors"] and all(
                s["key"].startswith("PF_SECTOR:") for s in risk["sectors"]
            )
            summary = client.get("/api/summary").json()
            assert summary["books"][0]["day_return_pct"] is not None
            steps = [(s["state"], s["at"][11:16]) for s in summary["schedule"]]
            assert steps[:3] == [("OPEN", "09:15"), ("ENTRY_WINDOW", "09:20"), ("MONITOR", "09:45")]
            assert steps[-1] == ("EXIT", "15:50")  # instants are IST
            watch = {w["symbol"]: w for w in client.get("/api/market/watchlist").json()}
            assert (
                watch["INFY"]["held"]
                and watch["INFY"]["ltp"]
                and "momentum BUY" in watch["INFY"]["signals_today"]
            )
        finally:
            engine.request_stop()
            while not run.done():
                await clock.advance(30)
            manager.close_reader()


async def test_lineage_executions_markers_equity_and_symbol_filters(session):
    engine, manager = session
    client = client_for(manager)
    decision = client.get("/api/decisions", params={"symbol": "INFY", "book": "A",
                                                    "outcome": "submitted"}).json()[0]  # fmt: skip
    lineage = client.get(f"/api/decisions/{decision['decision_id']}").json()
    entries = [x for x in lineage["executions"] if x["kind"] == "open"]
    assert {x["book_id"] for x in entries} == {"A", "B", "C"}
    for x in entries:
        assert x["status"] == "FILLED" and x["filled_qty"] == x["quantity"]
        assert x["slippage_vs_arrival_bps"] is not None and Decimal(x["charges"]) > 0
    assert any(x["kind"] != "open" for x in lineage["executions"])  # the exit leg
    bars = client.get("/api/market/INFY/bars").json()
    kinds = [m["kind"] for m in bars["markers"]]
    assert kinds.count("entry") == 3 and kinds.count("exit") == 3
    assert {o["symbol"] for o in client.get("/api/orders", params={"symbol": "TCS"}).json()} == {
        "TCS"
    }
    assert {f["symbol"] for f in client.get("/api/fills", params={"symbol": "INFY"}).json()} == {
        "INFY"
    }
    assert client.get("/api/trades", params={"symbol": "NOPE"}).json() == []
    equity = client.get("/api/equity").json()
    assert [b["book_id"] for b in equity["books"]] == ["A", "B", "C"]
    a_point = equity["books"][0]["points"][-1]
    assert a_point["date"] == DAY.isoformat() and a_point["peak"] >= a_point["equity"]
    assert equity["benchmark"] == "^NSEI"
    assert equity["benchmark_points"][0]["return_pct"] == 0  # its base: the session before


async def test_alerts_logs_reports_documents_and_calibration(session, settings):
    engine, manager = session
    from src.domain.events import Alert
    from src.store.sink import StoreSink

    StoreSink(engine.store, engine.clock, "test").emit(
        Alert(level="CRITICAL", key="feed_down", message="token sk-ant-abcdef1234567890 leaked")
    )
    client = client_for(manager)
    critical = client.get("/api/alerts", params={"level": "CRITICAL"}).json()
    assert critical[0]["key"] == "feed_down" and "sk-ant-" not in critical[0]["message"]
    assert client.get("/api/alerts", params={"level": "LOUD"}).status_code == 422

    logs_dir = manager.settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    lines = [{"ts": "2026-10-05T04:00:00+00:00", "level": "INFO", "logger": "src.engine", "msg": "started"},
             {"ts": "2026-10-05T04:01:00+00:00", "level": "WARNING", "logger": "src.marketdata",
              "msg": "auth gsk_secretvalue failed"}, "not json"]  # fmt: skip
    (logs_dir / "rakshaquant-20261005.log").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines), encoding="utf-8"
    )
    logs = client.get("/api/logs").json()
    assert [x["level"] for x in logs] == ["WARNING", "INFO"]  # newest first, junk skipped
    assert "gsk_" not in logs[0]["message"]
    assert len(client.get("/api/logs", params={"contains": "STARTED"}).json()) == 1

    reports = manager.settings.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{DAY}.json").write_text(json.dumps({
        "date": DAY.isoformat(), "experiment": "month1",
        "books": {"B": {"capital": {"end_equity": 1005030.0}, "pnl": {"day_return_pct": 0.26},
                        "trades": {"exits": 1}}},
        "comparison": {"B": {"net_ai_value_inr": 820.5}}}), encoding="utf-8")  # fmt: skip
    (reports / f"{DAY}.md").write_text("# Daily report\n", encoding="utf-8")
    listed = client.get("/api/reports").json()
    assert listed[0]["books"]["B"]["net_ai_value_inr"] == 820.5
    assert client.get(f"/api/reports/{DAY}/markdown").json()["markdown"] == "# Daily report\n"
    assert client.get("/api/reports/2026-10-06/markdown").status_code == 404
    prereg = client.get("/api/docs/preregistration").json()
    assert prereg["title"].startswith("Pre-registration") and "H2" in prereg["markdown"]
    assert client.get("/api/ai/calibration").json() == {"fitted": False, "temperatures": {},
                                                        "meta": {}}  # fmt: skip


async def test_a_resting_stop_has_no_arrival_slippage(session):
    """Its arrival quote is from when it was placed, long before it triggered."""
    engine, manager = session
    client = client_for(manager)
    decision = client.get("/api/decisions", params={"symbol": "TCS", "book": "A",
                                                    "outcome": "submitted"}).json()[0]  # fmt: skip
    executions = client.get(f"/api/decisions/{decision['decision_id']}").json()["executions"]
    stop = next(x for x in executions if x["kind"] != "open")
    assert stop["arrival_price"] is None and stop["slippage_vs_arrival_bps"] is None
    assert stop["slippage_vs_decision_bps"] is not None  # vs its trigger
    entry = next(x for x in executions if x["kind"] == "open")
    assert entry["slippage_vs_arrival_bps"] is not None
