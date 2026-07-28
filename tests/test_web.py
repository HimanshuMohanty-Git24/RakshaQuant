"""
Smoke tests for the web console: the snapshot/trace serialisation contract, the run-control
guards, and the FastAPI REST + WebSocket surface. No real trading run is started here.
"""

import os
from types import SimpleNamespace

import pytest

# Ensure settings can be constructed regardless of the caller's environment.
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("LANGSMITH_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from src.dashboard.cli import TradingStats  # noqa: E402
from src.live.recorder import CycleRecorder, env_badge, snapshot_from_stats  # noqa: E402
from src.live.views import StreamSessionView  # noqa: E402
from src.web.run_manager import RunControlError, RunManager, resolve_effective_mode  # noqa: E402
from src.web.server import create_app  # noqa: E402


class _CollectSink:
    """Minimal SnapshotSink for exercising StreamSessionView without a server."""

    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self.cycles: list[dict] = []

    def set_snapshot(self, snapshot: dict) -> None:
        self.snapshots.append(snapshot)

    def add_cycle(self, cycle: dict) -> None:
        self.cycles.append(cycle)

# ── Serialisation contract ──────────────────────────────────────────────────────


def test_env_badge_mapping():
    assert env_badge("local_paper") == "PAPER"
    assert env_badge("shadow") == "SHADOW"
    assert env_badge("dhan_paper") == "SHADOW"
    assert env_badge("live") == "LIVE"


def test_snapshot_shape_and_env():
    snap = snapshot_from_stats(TradingStats(), run_status="IDLE", effective_mode="local_paper")
    for key in (
        "run", "account", "trades", "agents", "regime",
        "finops", "goal", "positions", "quotes", "decision", "activity",
    ):
        assert key in snap
    assert snap["run"]["env"] == "PAPER"
    assert snap["run"]["status"] == "IDLE"


def test_resolve_effective_mode_downgrades_live_without_gate():
    assert resolve_effective_mode(SimpleNamespace(execution_mode="live", allow_live_orders=False)) == "shadow"
    assert resolve_effective_mode(SimpleNamespace(execution_mode="live", allow_live_orders=True)) == "live"
    assert (
        resolve_effective_mode(SimpleNamespace(execution_mode="local_paper", allow_live_orders=False))
        == "local_paper"
    )


def test_cycle_recorder_builds_pipeline_spans():
    rec = CycleRecorder()
    rec.begin({"market_regime": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}})
    final_state = {
        "regime": "trending_up",
        "regime_confidence": 0.7,
        "active_strategies": ["momentum"],
        "validated_signals": [{"symbol": "X"}],
        "rejected_signals": [],
        "approved_trades": [{"symbol": "X"}],
        "risk_rejected": [],
        "risk_warnings": [],
    }
    after = {"market_regime": {"input_tokens": 110, "output_tokens": 45, "cost_usd": 0.01}}
    trace = rec.finish(
        workflow_id="WF-1", final_state=final_state, signals_count=1, by_agent_after=after
    )

    assert [s.name for s in trace.spans] == [
        "support_agents",
        "market_regime",
        "strategy_selection",
        "signal_validation",
        "risk_compliance",
    ]
    regime_span = next(s for s in trace.spans if s.name == "market_regime")
    assert regime_span.input_tokens == 100 and regime_span.output_tokens == 40
    # Deterministic node carries no tokens/cost (honest signal that no LLM was called).
    risk_span = next(s for s in trace.spans if s.name == "risk_compliance")
    assert risk_span.total_tokens == 0 and risk_span.cost_usd == 0.0
    assert trace.approved_count == 1


# ── RunManager (no run started) ─────────────────────────────────────────────────


def test_run_manager_sink_and_state():
    m = RunManager()
    assert m.is_running is False
    m.set_snapshot({"tick": 1})
    m.add_cycle({"id": "c1"})
    state = m.state()
    assert state["snapshot"] == {"tick": 1}
    assert state["cycles"] == [{"id": "c1"}]
    assert m.cycle("c1") == {"id": "c1"}
    assert m.cycle("missing") is None


async def test_run_manager_readonly_guard(monkeypatch):
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    with pytest.raises(RunControlError):
        await RunManager().start(demo=True)


async def test_run_manager_readonly_blocks_stop(monkeypatch):
    # Read-only disables run-control ENTIRELY — stopping is a control action too.
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    with pytest.raises(RunControlError):
        await RunManager().stop()


def test_stream_view_note_strips_rich_markup():
    view = StreamSessionView(TradingStats(), _CollectSink(), effective_mode="local_paper")
    view.note("[bold green]RakshaQuant Live Trading System Starting...[/]")
    view.note("[dim]Mode: SIMULATED | Press Ctrl+C to stop[/]")
    messages = [e["message"] for e in view.stats.activity_log]
    assert "RakshaQuant Live Trading System Starting..." in messages
    assert "Mode: SIMULATED | Press Ctrl+C to stop" in messages
    # No leftover markup brackets reached the feed.
    assert not any("[" in m or "]" in m for m in messages)


async def test_run_manager_subscribe_sends_init_frame():
    m = RunManager()
    gen = m.subscribe()
    try:
        msg = await anext(gen)
        assert msg["type"] == "init"
        assert msg["running"] is False
    finally:
        await gen.aclose()


# ── FastAPI surface ─────────────────────────────────────────────────────────────


def test_rest_endpoints():
    client = TestClient(create_app())
    assert client.get("/api/health").json() == {"status": "ok", "running": False}

    state = client.get("/api/state").json()
    assert state["running"] is False and state["cycles"] == []

    cfg = client.get("/api/config").json()
    assert "env" in cfg and "allowLiveOrders" in cfg and "effectiveMode" in cfg

    assert client.get("/api/cycles").json() == {"cycles": []}
    assert client.get("/api/cycles/nope").status_code == 404


def test_websocket_init_contract():
    client = TestClient(create_app())
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "init"
        assert msg["running"] is False
        assert msg["snapshot"] is None


def test_run_stop_readonly_returns_409(monkeypatch):
    # The stop endpoint must translate RunControlError to 409, not surface a 500.
    monkeypatch.setenv("RAKSHAQUANT_WEB_READONLY", "1")
    client = TestClient(create_app())
    res = client.post("/api/run/stop")
    assert res.status_code == 409
    assert "error" in res.json()
