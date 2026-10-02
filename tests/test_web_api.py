"""Plan M9.2: the REST projections, over a real three-book session (tests/test_books.py)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.config.limits import load_risk_limits
from src.domain.events import MarkToMarket
from src.web.run_manager import RunManager
from src.web.server import create_app
from tests.test_books import run as run_day
from tests.test_books import three_books
from tests.test_engine_replay import DAY, INFY, TCS
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
