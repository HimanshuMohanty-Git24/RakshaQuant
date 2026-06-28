"""
Tests for the live broker lifecycle and reconciliation (all mocked — no real broker calls),
plus the ExecutionService live submission path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.adapter import OrderRequest, OrderResult, OrderSide, OrderStatus
from src.execution.costs import CostModel
from src.execution.live_executor import LiveBrokerExecutor, reconcile_positions
from src.execution.paper_engine import LocalPaperEngine
from src.execution.service import ExecutionMode, ExecutionService, IdempotencyStore


def _req():
    return OrderRequest("AAPL", "NSE", OrderSide.BUY, 10)


def _result(status, filled=0, avg=0.0, oid="O1"):
    return OrderResult(order_id=oid, request=_req(), status=status,
                       filled_quantity=filled, average_price=avg)


# ---------------------------------------------------------------------------
# LiveBrokerExecutor.place_and_confirm
# ---------------------------------------------------------------------------

async def test_place_and_confirm_filled():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_result(OrderStatus.PLACED))
    adapter.get_order_status = AsyncMock(return_value=_result(OrderStatus.FILLED, 10, 150.0))
    ex = LiveBrokerExecutor(adapter=adapter, poll_attempts=3, poll_delay=0)

    res = await ex.place_and_confirm(_req(), "COID")
    assert res.status == OrderStatus.FILLED
    assert res.filled_quantity == 10
    assert res.average_price == 150.0
    adapter.place_order.assert_awaited_once()


async def test_place_and_confirm_partial():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_result(OrderStatus.PLACED))
    adapter.get_order_status = AsyncMock(
        return_value=_result(OrderStatus.PARTIALLY_FILLED, 4, 150.0)
    )
    ex = LiveBrokerExecutor(adapter=adapter, poll_attempts=3, poll_delay=0)

    res = await ex.place_and_confirm(_req())
    assert res.status == OrderStatus.PARTIALLY_FILLED
    assert res.filled_quantity == 4


async def test_place_and_confirm_rejected_on_submit_skips_polling():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_result(OrderStatus.REJECTED))
    adapter.get_order_status = AsyncMock()
    ex = LiveBrokerExecutor(adapter=adapter, poll_attempts=3, poll_delay=0)

    res = await ex.place_and_confirm(_req())
    assert res.status == OrderStatus.REJECTED
    adapter.get_order_status.assert_not_awaited()


async def test_place_and_confirm_unconfirmed_within_budget():
    adapter = MagicMock()
    adapter.place_order = AsyncMock(return_value=_result(OrderStatus.PLACED))
    # PENDING is never terminal, so polling exhausts the budget.
    adapter.get_order_status = AsyncMock(return_value=_result(OrderStatus.PENDING))
    ex = LiveBrokerExecutor(adapter=adapter, poll_attempts=2, poll_delay=0)

    res = await ex.place_and_confirm(_req())
    assert res.status == OrderStatus.PLACED
    assert "unconfirmed" in res.message


# ---------------------------------------------------------------------------
# reconcile_positions
# ---------------------------------------------------------------------------

def test_reconcile_in_sync():
    local = [{"symbol": "A", "quantity": 10, "side": "BUY"}]
    broker = [{"symbol": "A", "quantity": 10, "side": "BUY"}]
    report = reconcile_positions(local, broker)
    assert report.in_sync is True


def test_reconcile_broker_only_and_local_only():
    report = reconcile_positions(
        [{"symbol": "L", "quantity": 5, "side": "BUY"}],
        [{"symbol": "B", "quantity": 5, "side": "BUY"}],
    )
    assert report.in_sync is False
    assert report.broker_only == ["B"]
    assert report.local_only == ["L"]


def test_reconcile_quantity_mismatch():
    report = reconcile_positions(
        [{"symbol": "A", "quantity": 10, "side": "BUY"}],
        [{"symbol": "A", "quantity": 5, "side": "BUY"}],
    )
    assert report.in_sync is False
    assert report.quantity_mismatches[0]["local_qty"] == 10
    assert report.quantity_mismatches[0]["broker_qty"] == 5


def test_reconcile_accepts_position_objects():
    local = [SimpleNamespace(symbol="A", quantity=10, side="BUY")]
    broker = [{"symbol": "A", "quantity": 10, "side": "BUY"}]
    assert reconcile_positions(local, broker).in_sync is True


# ---------------------------------------------------------------------------
# ExecutionService live submission (mocked broker)
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    return LocalPaperEngine(
        initial_balance=100_000.0, state_file=tmp_path / "w.json", cost_model=CostModel.zero()
    )


def _live_service(engine, broker_executor):
    fake_settings = MagicMock(dhan_client_id="id", dhan_access_token="tok")
    with patch("src.execution.service.get_settings", return_value=fake_settings):
        return ExecutionService(
            engine=engine, mode=ExecutionMode.LIVE, allow_live_orders=True,
            idempotency=IdempotencyStore(), broker_executor=broker_executor,
        )


async def test_submit_async_live_filled(engine):
    broker = MagicMock()
    broker.place_and_confirm = AsyncMock(
        return_value=_result(OrderStatus.FILLED, filled=10, avg=150.0, oid="O9")
    )
    svc = _live_service(engine, broker)
    assert svc.effective_mode == ExecutionMode.LIVE

    res = await svc.submit_async(
        symbol="AAPL", side="BUY", quantity=10, price=150.0, idempotency_key="L1"
    )
    assert res.status == "FILLED"
    assert res.order_id == "O9"
    assert res.fill_price == 150.0
    broker.place_and_confirm.assert_awaited_once()


async def test_submit_async_live_without_executor_rejects(engine):
    svc = _live_service(engine, broker_executor=None)
    res = await svc.submit_async(
        symbol="AAPL", side="BUY", quantity=10, price=150.0, idempotency_key="L2"
    )
    assert res.status == "REJECTED"
    assert "no broker executor" in res.message


async def test_submit_async_paper_still_works(engine):
    svc = ExecutionService(
        engine=engine, mode=ExecutionMode.LOCAL_PAPER, idempotency=IdempotencyStore()
    )
    res = await svc.submit_async(
        symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="P1"
    )
    assert res.filled
    # Dedup applies on the async path too.
    dup = await svc.submit_async(
        symbol="AAPL", side="BUY", quantity=10, price=100.0, idempotency_key="P1"
    )
    assert dup.is_duplicate
