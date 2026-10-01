"""Plan M2.6: simulated or synthetic prices can never create orders outside the demo environment."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from src.dashboard.cli import TradingStats
from src.domain.types import CheckOutcome, MarketDataSource, ReasonCode
from src.execution.costs import CostModel
from src.execution.paper_engine import LocalPaperEngine
from src.execution.service import ExecutionService, IdempotencyStore
from src.live.session import run_trading_session
from src.risk.checks.system import check_data_source, legacy_source

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source", "environment", "outcome"),
    [
        (MarketDataSource.SIMULATED, "paper", CheckOutcome.BLOCK),
        (MarketDataSource.SYNTHETIC, "dev", CheckOutcome.BLOCK),
        (MarketDataSource.SIMULATED, "test", CheckOutcome.BLOCK),
        (MarketDataSource.SIMULATED, "demo", CheckOutcome.ALLOW),
        (MarketDataSource.YFINANCE, "paper", CheckOutcome.ALLOW),
        (MarketDataSource.REPLAY, "paper", CheckOutcome.ALLOW),
        (None, "paper", CheckOutcome.BLOCK),
    ],
)
def test_data_source_check(source, environment, outcome):
    result = check_data_source(source, environment)
    assert result.outcome is outcome and result.code is ReasonCode.SYS_DATA_SIMULATED


def test_legacy_source_names():
    assert legacy_source("simulated") is MarketDataSource.SIMULATED
    assert legacy_source("yfinance") is MarketDataSource.YFINANCE
    assert legacy_source("something-else") is None


def _service(tmp_path: Path, source: MarketDataSource, environment: str) -> ExecutionService:
    engine = LocalPaperEngine(
        initial_balance=1_000_000, state_file=tmp_path / "wallet.json", cost_model=CostModel.zero()
    )
    return ExecutionService(
        engine=engine,
        idempotency=IdempotencyStore(),
        data_guard=lambda: check_data_source(source, environment),
    )


async def test_simulated_source_in_paper_environment_creates_zero_orders(tmp_path):
    svc = _service(tmp_path, MarketDataSource.SIMULATED, "paper")
    sync = svc.submit(symbol="INFY", side="BUY", quantity=10, price=1500.0, idempotency_key="k1")
    async_ = await svc.submit_async(
        symbol="TCS", side="BUY", quantity=5, price=3000.0, idempotency_key="k2"
    )
    for result in (sync, async_):
        assert result.status == "BLOCKED"
        assert result.message.startswith("SYS_DATA_SIMULATED")
    assert svc.engine.get_positions() == [] and svc.engine.orders == []
    assert not (tmp_path / "wallet.json").exists()


def test_demo_environment_may_trade_simulated_prices(tmp_path):
    svc = _service(tmp_path, MarketDataSource.SIMULATED, "demo")
    result = svc.submit(symbol="INFY", side="BUY", quantity=10, price=1500.0, idempotency_key="k1")
    assert result.status == "FILLED" and len(svc.engine.get_positions()) == 1


class _View:
    def __init__(self) -> None:
        self.stats = TradingStats()
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)


class _StopHereError(Exception):
    pass


def _boom(*_: Any, **__: Any) -> Any:
    raise _StopHereError


async def test_legacy_session_refuses_simulated_data_outside_demo(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "paper")
    view = _View()
    with (
        patch("src.live.session.is_market_open", return_value=False),
        patch("src.live.session.create_trading_graph", side_effect=_boom),
        patch("src.live.session.LocalPaperEngine", side_effect=_boom),
    ):
        await run_trading_session(view)  # returns: nothing heavy ran, no wallet was opened
    assert any("Not trading" in n and "--demo" in n for n in view.notes)


async def test_legacy_session_proceeds_in_demo(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "demo")
    with (
        patch("src.live.session.is_market_open", return_value=False),
        patch("src.live.session.setup_tracing", side_effect=_boom),
    ):
        with pytest.raises(_StopHereError):  # got past the refusal into the normal start-up
            await run_trading_session(_View())


def _entry() -> ModuleType:
    path = REPO_ROOT / "scripts" / "run_live_trading.py"
    spec = importlib.util.spec_from_file_location("run_live_trading_demo_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv", [["--demo"], ["--mode", "web", "--demo"]])
def test_demo_flag_selects_the_demo_environment(monkeypatch, argv):
    entry = _entry()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sys, "argv", ["run_live_trading.py", *argv])
    monkeypatch.setattr(entry, "run_entry_point", lambda name, main, **kw: captured.update(kw))
    entry.main()
    assert os.environ["ENVIRONMENT"] == "demo"
    assert captured["lock"] is True  # the demo environment has its own lock and state
