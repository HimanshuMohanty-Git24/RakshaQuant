"""
Tests for the durability/consistency cleanup:

- #3 unified lesson-decay formula (database.decayed_score; scheduler delegates to it)
- #2 ExitManager state persistence across restarts
- #1 TradeJournal close_trade net-PnL / partial / zero-guard + persistence flag
"""

from pathlib import Path

from src.execution.exit_manager import ExitManager
from src.execution.journal import TradeJournal
from src.memory.database import decayed_score

# ---------------------------------------------------------------------------
# #3 — unified decay
# ---------------------------------------------------------------------------

def test_decay_full_score_within_grace_period():
    assert decayed_score(2.0, age_days=10, decay_days=30) == 2.0
    assert decayed_score(2.0, age_days=30, decay_days=30) == 2.0


def test_decay_after_grace_period():
    # One week (7 days) beyond the grace period -> one 0.95 step.
    assert decayed_score(2.0, age_days=37, decay_days=30) == round(2.0 * 0.95, 6)


def test_decay_is_monotonic():
    older = decayed_score(2.0, age_days=100, decay_days=30)
    newer = decayed_score(2.0, age_days=50, decay_days=30)
    assert older < newer < 2.0


# ---------------------------------------------------------------------------
# #2 — ExitManager persistence
# ---------------------------------------------------------------------------

def test_exit_manager_persists_positions(tmp_path):
    path = tmp_path / "exit_state.json"
    em1 = ExitManager(state_file=path)
    em1.register_position(
        position_id="P1", symbol="X", side="BUY", quantity=10,
        entry_price=100.0, stop_loss=95.0, target_price=110.0,
        strategy="momentum", regime="trending_up",
    )
    # A price update should be persisted (MFE moves).
    em1.check_exits({"X": 105.0})

    em2 = ExitManager(state_file=path)
    pos = em2.get_position("P1")
    assert pos is not None
    assert pos.entry_price == 100.0
    assert pos.stop_loss == 95.0
    assert pos.strategy == "momentum"
    assert pos.mfe > 0  # the trailing/MFE update survived the restart


def test_exit_manager_unregister_persists(tmp_path):
    path = tmp_path / "exit_state.json"
    em1 = ExitManager(state_file=path)
    em1.register_position(
        position_id="P1", symbol="X", side="BUY", quantity=1,
        entry_price=100.0, stop_loss=95.0, target_price=110.0,
    )
    em1.unregister_position("P1")
    em2 = ExitManager(state_file=path)
    assert em2.get_position("P1") is None


# ---------------------------------------------------------------------------
# #1 — TradeJournal close_trade
# ---------------------------------------------------------------------------

def _journal():
    return TradeJournal(database_url="sqlite:///:memory:")


def test_close_trade_uses_provided_net_pnl():
    j = _journal()
    tid = j.record_trade(
        {"symbol": "A", "entry_price": 100, "quantity": 10, "signal_type": "BUY"}, "wf", {}
    )
    assert j.close_trade(tid, 110.0, "target", pnl=97.9) is True
    record = j.get_trade(tid)
    assert record["profit_loss"] == 97.9  # net (provided), not the gross 100


def test_close_trade_partial_quantity():
    j = _journal()
    tid = j.record_trade(
        {"symbol": "B", "entry_price": 100, "quantity": 10, "signal_type": "BUY"}, "wf", {}
    )
    assert j.close_trade(tid, 110.0, "partial", pnl=48.95, exit_quantity=5) is True
    assert j.get_trade(tid)["profit_loss"] == 48.95


def test_close_trade_zero_entry_price_does_not_crash():
    j = _journal()
    tid = j.record_trade(
        {"symbol": "C", "entry_price": 0, "quantity": 1, "signal_type": "BUY"}, "wf", {}
    )
    assert j.close_trade(tid, 10.0, "x") is True  # no ZeroDivisionError
    assert j.get_trade(tid)["profit_loss_pct"] == 0.0


def test_journal_persistence_flag():
    assert _journal().is_persistent is False  # in-memory
    file_journal = TradeJournal(database_url="sqlite:///" + str(Path("dummy_journal.db")))
    assert file_journal.is_persistent is True
