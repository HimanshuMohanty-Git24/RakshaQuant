"""Plan M3.6: the legacy order paths are retired (and the legacy loop no longer oversells)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.execution.adapter import execute_trades
from src.execution.costs import CostModel
from src.execution.exit_manager import ExitManager as LegacyExitManager
from src.execution.paper_engine import LocalPaperEngine
from src.live.session import _held_quantity

SRC = Path(__file__).resolve().parents[1] / "src"


def test_no_direct_paper_engine_orders_anywhere_in_src():
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "paper_engine.place_order" in line
    ]
    assert offenders == []


def test_execute_trades_is_retired():
    with pytest.raises(NotImplementedError, match="OMS.submit"):
        asyncio.run(execute_trades([{"symbol": "INFY"}]))


def test_held_quantity_counts_only_the_given_side(tmp_path):
    engine = LocalPaperEngine(
        initial_balance=1_000_000, state_file=tmp_path / "w.json", cost_model=CostModel.zero()
    )
    engine.place_order(symbol="INFY", side="BUY", quantity=60, current_price=1000.0)
    engine.place_order(symbol="INFY", side="BUY", quantity=40, current_price=1001.0)
    assert _held_quantity(engine, "INFY", "BUY") == 100
    assert _held_quantity(engine, "INFY", "SELL") == 0
    assert _held_quantity(engine, "TCS", "BUY") == 0


def test_legacy_partial_exit_shrinks_the_managed_quantity(tmp_path):
    """Audit F-01 in the legacy loop: a partial exit must decrement what is managed, so the
    final exit sells the remainder, not the original quantity."""
    manager = LegacyExitManager(state_file=tmp_path / "exits.json", partial_profit_r=1.0)
    manager.register_position(
        position_id="p1",
        symbol="INFY",
        side="BUY",
        quantity=100,
        entry_price=1000.0,
        stop_loss=979.0,
        target_price=1063.0,
    )
    manager.record_partial("p1", 50)
    (pos,) = [p for p in manager._positions.values() if p.position_id == "p1"]
    assert pos.quantity == 50 and pos.partial_taken
    # No second partial at the next 1R touch.
    rules = [rule.exit_type for _, rule in manager.check_exits({"INFY": 1022.0}, "", {})]
    assert "partial" not in rules
    manager.record_partial("p1", 50)
    assert "p1" not in manager._positions  # fully exited: no longer managed
