"""
Tests for the deterministic tail-risk guards:
- DrawdownTracker (incl. unrealized) and that its output now actually trips the kill switch
  (the live loop previously fed a hardcoded 0, so the drawdown limb never fired).
- is_circuit_locked NSE price-band detection.
- ExitManager.clear() used by the kill-switch flatten.
"""

from src.agents.risk_compliance import RiskLimits, check_kill_switch
from src.execution.costs import CostModel
from src.execution.exit_manager import ExitManager
from src.execution.paper_engine import LocalPaperEngine
from src.risk.guards import DrawdownTracker, is_circuit_locked

# ---------------------------------------------------------------------------
# DrawdownTracker
# ---------------------------------------------------------------------------

def test_drawdown_tracker_tracks_peak_to_trough():
    t = DrawdownTracker(peak_equity=1_000_000, current_equity=1_000_000)
    t.update(1_010_000)  # new peak
    assert t.peak_equity == 1_010_000
    assert t.max_drawdown == 0

    t.update(960_000)  # 50k below peak
    assert t.max_drawdown == 50_000
    assert round(t.drawdown_pct, 4) == round(50_000 / 1_010_000 * 100, 4)

    t.update(1_005_000)  # partial recovery — max drawdown is sticky
    assert t.max_drawdown == 50_000


def test_drawdown_includes_unrealized_loss_immediately():
    t = DrawdownTracker(peak_equity=1_000_000, current_equity=1_000_000)
    t.update(955_000)  # 4.5% unrealized loss, no realized trades yet
    assert round(t.drawdown_pct, 1) == 4.5


# ---------------------------------------------------------------------------
# Kill-switch wiring (the bug: drawdown limb was dead)
# ---------------------------------------------------------------------------

def _kill_state(tracker: DrawdownTracker) -> dict:
    return {
        "daily_stats": {"profit_loss": 0, "max_drawdown": tracker.max_drawdown},
        "portfolio": {"capital": max(tracker.peak_equity, 1.0)},
    }


def test_kill_switch_fires_on_real_drawdown():
    limits = RiskLimits(max_daily_loss=10_000_000.0, max_drawdown_pct=5.0)
    t = DrawdownTracker(peak_equity=1_000_000, current_equity=1_000_000)
    t.update(940_000)  # 6% drawdown, zero realized P&L
    assert check_kill_switch(_kill_state(t), limits) is True


def test_kill_switch_quiet_below_drawdown_limit():
    limits = RiskLimits(max_daily_loss=10_000_000.0, max_drawdown_pct=5.0)
    t = DrawdownTracker(peak_equity=1_000_000, current_equity=1_000_000)
    t.update(970_000)  # 3% drawdown
    assert check_kill_switch(_kill_state(t), limits) is False


# ---------------------------------------------------------------------------
# Circuit guard
# ---------------------------------------------------------------------------

def test_is_circuit_locked():
    assert is_circuit_locked(10.0, 10.0) is True
    assert is_circuit_locked(-12.0, 10.0) is True
    assert is_circuit_locked(7.5, 10.0) is False
    assert is_circuit_locked(50.0, 0) is False  # band 0 disables the check


# ---------------------------------------------------------------------------
# ExitManager.clear (kill-switch flatten)
# ---------------------------------------------------------------------------

def test_exit_manager_clear_drops_and_persists(tmp_path):
    path = tmp_path / "exits.json"
    em = ExitManager(state_file=path)
    em.register_position(
        position_id="P1", symbol="X", side="BUY", quantity=1,
        entry_price=100.0, stop_loss=95.0, target_price=110.0,
    )
    assert em.get_position("P1") is not None
    em.clear()
    assert em.get_position("P1") is None
    # Persisted: a fresh manager loads nothing.
    assert ExitManager(state_file=path).get_position("P1") is None


# ---------------------------------------------------------------------------
# Kill-switch flatten: loop until truly flat
# ---------------------------------------------------------------------------

def test_flatten_loop_clears_multiple_same_symbol_positions(tmp_path):
    """The flatten loop must reach a flat book before tracking is cleared.

    LocalPaperEngine mints a fresh position id per open, so two BUYs on one symbol are TWO
    positions and place_order() closes by (symbol, side) one match at a time. A single
    snapshot pass can leave residuals (or even open a short on a size mismatch), so the
    kill-switch flatten loops until the engine reports flat — this guards that property.
    """
    eng = LocalPaperEngine(
        initial_balance=1_000_000.0,
        state_file=tmp_path / "wallet.json",
        cost_model=CostModel.zero(),
    )
    eng.place_order("AAA", "BUY", 10, 100.0)
    eng.place_order("AAA", "BUY", 5, 102.0)  # second, odd-sized same-symbol long
    eng.place_order("BBB", "BUY", 7, 50.0)
    assert len(eng.get_positions()) == 3

    # Mirror the kill-switch flatten: opposite order per snapshot position, until flat (capped).
    for _ in range(20):
        positions = list(eng.get_positions())
        if not positions:
            break
        for pos in positions:
            eng.place_order(
                pos.symbol,
                "SELL" if pos.side == "BUY" else "BUY",
                pos.quantity,
                pos.current_price or pos.entry_price,
            )

    assert eng.get_positions() == []  # fully flat → only now is it safe to clear tracking
