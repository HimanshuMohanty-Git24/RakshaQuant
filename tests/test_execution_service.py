"""
Tests for the unified ExecutionService: mode resolution (incl. shadow + no silent
downgrade), order idempotency, the kill-switch gate, and the IdempotencyStore.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.costs import CostModel
from src.execution.paper_engine import LocalPaperEngine
from src.execution.service import (
    ExecutionMode,
    ExecutionService,
    IdempotencyStore,
)


@pytest.fixture
def engine(tmp_path):
    return LocalPaperEngine(
        initial_balance=100_000.0,
        state_file=tmp_path / "wallet.json",
        cost_model=CostModel.zero(),
    )


def _service(engine, **kwargs):
    kwargs.setdefault("idempotency", IdempotencyStore())
    return ExecutionService(engine=engine, **kwargs)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def test_local_paper_fills(engine):
    svc = _service(engine, mode=ExecutionMode.LOCAL_PAPER)
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1")
    assert result.filled
    assert result.is_shadow is False
    assert len(engine.get_positions()) == 1


def test_live_without_opt_in_runs_shadow(engine):
    svc = _service(engine, mode=ExecutionMode.LIVE, allow_live_orders=False)
    assert svc.effective_mode == ExecutionMode.SHADOW
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1")
    assert result.filled  # simulated
    assert result.is_shadow is True
    assert "SHADOW" in result.message


def test_dhan_paper_without_opt_in_runs_shadow(engine):
    svc = _service(engine, mode=ExecutionMode.DHAN_PAPER, allow_live_orders=False)
    assert svc.effective_mode == ExecutionMode.SHADOW


def test_live_with_opt_in_but_no_creds_runs_shadow_not_local(engine):
    # allow_live_orders=True but no Dhan creds -> SHADOW (NOT a silent local-paper downgrade).
    svc = _service(engine, mode=ExecutionMode.LIVE, allow_live_orders=True)
    assert svc.effective_mode == ExecutionMode.SHADOW


def test_live_via_sync_submit_rejects_directs_to_async(engine):
    # Live orders are async (broker lifecycle); the sync submit() must refuse, never silently
    # fill the paper wallet. Real live submission goes through submit_async (see live tests).
    fake_settings = MagicMock(dhan_client_id="id", dhan_access_token="tok")
    with patch("src.execution.service.get_settings", return_value=fake_settings):
        svc = ExecutionService(
            engine=engine, mode=ExecutionMode.LIVE, allow_live_orders=True,
            idempotency=IdempotencyStore(),
        )
        assert svc.effective_mode == ExecutionMode.LIVE
        result = svc.submit(
            symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k1"
        )
    assert result.status == "REJECTED"
    assert "submit_async" in result.message
    assert len(engine.get_positions()) == 0


# ---------------------------------------------------------------------------
# Idempotency + kill switch
# ---------------------------------------------------------------------------

def test_duplicate_submission_is_suppressed(engine):
    svc = _service(engine, mode=ExecutionMode.LOCAL_PAPER)
    first = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="dup")
    second = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="dup")

    assert first.filled
    assert second.is_duplicate
    assert second.status == "DUPLICATE"
    # Engine only saw the first order — no double position / double spend.
    assert len(engine.get_positions()) == 1
    assert engine.get_balance() == 100_000.0 - 10 * 100.0


def test_kill_switch_blocks_submission(engine):
    svc = _service(engine, mode=ExecutionMode.LOCAL_PAPER, kill_switch=lambda: True)
    result = svc.submit(symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="k")
    assert result.status == "BLOCKED"
    assert len(engine.get_positions()) == 0  # nothing placed


def test_rejected_order_is_not_recorded_as_duplicate(engine):
    # First order rejected (insufficient balance) -> the key can be retried later.
    svc = _service(engine, mode=ExecutionMode.LOCAL_PAPER)
    rejected = svc.submit(
        symbol="AAPL", side="BUY", quantity=100_000, price=100.0, idempotency_key="retry"
    )
    assert rejected.status == "REJECTED"
    retry = svc.submit(symbol="AAPL", side="BUY", quantity=1, price=100.0, idempotency_key="retry")
    assert retry.filled
    assert retry.is_duplicate is False


# ---------------------------------------------------------------------------
# IdempotencyStore persistence
# ---------------------------------------------------------------------------

def test_idempotency_store_persists(tmp_path):
    path = tmp_path / "idem.json"
    store = IdempotencyStore(path=path)
    store.record("k1", {"fill_price": 100.0, "order_id": "O1"})

    reloaded = IdempotencyStore(path=path)
    assert reloaded.seen("k1") is not None
    assert reloaded.seen("k1")["order_id"] == "O1"
    assert reloaded.seen("missing") is None
