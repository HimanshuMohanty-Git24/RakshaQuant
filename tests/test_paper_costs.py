"""
Tests for the paper-trading cost model (slippage + NSE-style charges) and its effect on
paper-engine fills and net P&L.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.execution.costs import CostModel
from src.execution.paper_engine import LocalPaperEngine

# ---------------------------------------------------------------------------
# CostModel
# ---------------------------------------------------------------------------

def test_slippage_is_adverse():
    model = CostModel(slippage_bps=10)  # 0.10%
    assert model.fill_price(100.0, "BUY") == pytest.approx(100.10)
    assert model.fill_price(100.0, "SELL") == pytest.approx(99.90)


def test_charges_components():
    model = CostModel(brokerage_bps=10, statutory_bps=5, gst_pct=18)
    # notional 10,000: brokerage 10 + statutory 5 + gst (18% of 10) 1.8 = 16.8
    assert model.charges(10_000, "BUY") == pytest.approx(16.8)


def test_brokerage_cap_applies():
    model = CostModel(brokerage_bps=10, brokerage_max=5, statutory_bps=0, gst_pct=18)
    # brokerage capped at 5, gst 0.9 -> 5.9
    assert model.charges(10_000, "BUY") == pytest.approx(5.9)


def test_from_settings_reads_values():
    settings = SimpleNamespace(
        paper_slippage_bps=2.0, paper_brokerage_bps=3.0, paper_brokerage_max=20.0,
        paper_statutory_bps=5.0, paper_gst_pct=18.0,
    )
    model = CostModel.from_settings(settings)
    assert model.slippage_bps == 2.0
    assert model.brokerage_max == 20.0


def test_from_settings_tolerates_non_numeric():
    # A MagicMock's attributes aren't real numbers; from_settings must fall back to 0.
    model = CostModel.from_settings(MagicMock())
    assert model.slippage_bps == 0.0
    assert model.brokerage_bps == 0.0


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------

@pytest.fixture
def engine_factory(tmp_path):
    def _make(cost_model):
        return LocalPaperEngine(
            initial_balance=100_000.0,
            state_file=tmp_path / "wallet.json",
            cost_model=cost_model,
        )
    return _make


def test_net_pnl_is_after_charges(engine_factory):
    # Isolate charges (no slippage): brokerage 10 bps only.
    engine = engine_factory(CostModel(brokerage_bps=10))
    engine.place_order("AAPL", "BUY", 10, 100.0)   # open_charges = 1000 * 0.001 = 1.0
    engine.place_order("AAPL", "SELL", 10, 110.0)  # close_charges = 1100 * 0.001 = 1.1

    # gross 100, minus entry charge 1.0, minus exit charge 1.1 = 97.9
    assert engine.realized_pnl == pytest.approx(97.9)
    assert engine.get_balance() == pytest.approx(100_000.0 + 97.9)


def test_flat_round_trip_loses_to_slippage(engine_factory):
    # Buy and sell at the same screen price -> slippage makes it a small loss.
    engine = engine_factory(CostModel(slippage_bps=10))
    engine.place_order("AAPL", "BUY", 10, 100.0)   # fills 100.10
    engine.place_order("AAPL", "SELL", 10, 100.0)  # fills 99.90
    assert engine.realized_pnl == pytest.approx(-2.0)
    assert engine.losing_trades == 1


def test_buy_fill_reflects_slippage(engine_factory):
    engine = engine_factory(CostModel(slippage_bps=10))
    order = engine.place_order("AAPL", "BUY", 10, 100.0)
    assert order.price == pytest.approx(100.10)
    assert engine.get_positions()[0].entry_price == pytest.approx(100.10)
