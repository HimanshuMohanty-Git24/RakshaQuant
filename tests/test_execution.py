from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Mock settings before importing modules that use them
with patch("src.config.get_settings") as mock_get_settings:
    mock_settings = MagicMock()
    mock_settings.paper_wallet_balance = 100000.0
    mock_settings.database_url = "sqlite:///:memory:"
    mock_settings.trading_mode = "paper"
    mock_settings.execution_mode = "local_paper"
    mock_settings.max_position_size = 10000.0
    mock_settings.dhan_client_id = "test_id"
    mock_settings.dhan_access_token.get_secret_value.return_value = "test_token"
    mock_get_settings.return_value = mock_settings

    from src.execution.adapter import (
        ExecutionAdapter,
        LocalExecutionAdapter,
        OrderRequest,
        OrderSide,
        OrderStatus,
        execute_trades,
    )
    from src.execution.costs import CostModel
    from src.execution.journal import DecisionLog, TradeJournal
    from src.execution.paper_engine import LocalPaperEngine

# --- LocalPaperEngine Tests ---


@pytest.fixture
def paper_engine(tmp_path):
    state_file = tmp_path / "test_paper_wallet.json"
    # Zero-cost model so these tests check pure order/position mechanics.
    # Cost behaviour is covered separately in test_paper_costs.py.
    engine = LocalPaperEngine(
        initial_balance=100000.0, state_file=state_file, cost_model=CostModel.zero()
    )
    return engine


def test_engine_init(paper_engine):
    assert paper_engine.get_balance() == 100000.0
    assert len(paper_engine.get_positions()) == 0


def test_place_order_buy_market(paper_engine):
    order = paper_engine.place_order(
        symbol="AAPL", side="BUY", quantity=10, current_price=150.0, order_type="MARKET"
    )

    assert order.status == "FILLED"
    assert paper_engine.get_balance() == 100000.0 - (10 * 150.0)
    assert len(paper_engine.get_positions()) == 1

    pos = paper_engine.get_positions()[0]
    assert pos.symbol == "AAPL"
    assert pos.quantity == 10
    assert pos.entry_price == 150.0


def test_place_order_insufficient_balance(paper_engine):
    order = paper_engine.place_order(
        symbol="AAPL",
        side="BUY",
        quantity=100000,  # Too many
        current_price=150.0,
    )
    assert order.status == "REJECTED"
    assert paper_engine.get_balance() == 100000.0


def test_place_order_sell_close(paper_engine):
    # Buy first
    paper_engine.place_order("AAPL", "BUY", 10, 150.0)

    # Sell
    order = paper_engine.place_order("AAPL", "SELL", 10, 160.0)

    assert order.status == "FILLED"
    assert len(paper_engine.get_positions()) == 0
    assert paper_engine.realized_pnl == 10 * (160.0 - 150.0)
    assert paper_engine.winning_trades == 1


def test_place_order_sell_short(paper_engine):
    # Sell without an existing long now OPENS a tracked short position (no cash leak).
    order = paper_engine.place_order("AAPL", "SELL", 10, 150.0)

    assert order.status == "FILLED"
    # Opening commits capital (notional), it is not credited to the balance.
    assert paper_engine.get_balance() == 100000.0 - (10 * 150.0)
    positions = paper_engine.get_positions()
    assert len(positions) == 1
    assert positions[0].side == "SELL"
    assert positions[0].quantity == 10


def test_short_round_trip_profit_when_price_falls(paper_engine):
    # Open short at 150, cover at 140 -> profit of 10/share.
    paper_engine.place_order("AAPL", "SELL", 10, 150.0)
    paper_engine.place_order("AAPL", "BUY", 10, 140.0)

    assert len(paper_engine.get_positions()) == 0
    assert paper_engine.realized_pnl == 10 * (150.0 - 140.0)
    assert paper_engine.winning_trades == 1


def test_partial_close_reduces_position(paper_engine):
    paper_engine.place_order("AAPL", "BUY", 10, 100.0)
    # Sell half.
    paper_engine.place_order("AAPL", "SELL", 4, 110.0)

    positions = paper_engine.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 6
    assert paper_engine.realized_pnl == 4 * (110.0 - 100.0)


def test_update_positions_pnl(paper_engine):
    paper_engine.place_order("AAPL", "BUY", 10, 150.0)

    paper_engine.update_positions_pnl({"AAPL": 160.0})

    pos = paper_engine.get_positions()[0]
    assert pos.current_price == 160.0
    assert pos.unrealized_pnl == 100.0
    assert abs(pos.unrealized_pnl_pct - (100 / (150 * 10) * 100)) < 0.01


def test_get_stats(paper_engine):
    paper_engine.place_order("AAPL", "BUY", 10, 150.0)

    stats = paper_engine.get_stats()
    assert stats["initial_balance"] == 100000.0
    assert stats["open_positions"] == 1


def test_persistence(tmp_path):
    state_file = tmp_path / "persist.json"
    engine1 = LocalPaperEngine(
        initial_balance=1000.0, state_file=state_file, cost_model=CostModel.zero()
    )
    engine1.place_order("AAPL", "BUY", 1, 100.0)

    # Load new engine from same file
    engine2 = LocalPaperEngine(state_file=state_file, cost_model=CostModel.zero())
    assert engine2.get_balance() == 900.0
    assert len(engine2.get_positions()) == 1


def test_reset(paper_engine):
    paper_engine.place_order("AAPL", "BUY", 10, 150.0)
    paper_engine.reset()
    assert paper_engine.get_balance() == 100000.0
    assert len(paper_engine.get_positions()) == 0


# --- TradeJournal Tests ---


@pytest.fixture
def trade_journal():
    return TradeJournal(database_url="sqlite:///:memory:")


def test_record_trade(trade_journal):
    trade = {
        "signal_id": "sig1",
        "symbol": "AAPL",
        "entry_price": 150.0,
        "quantity": 10,
        "confidence": 0.9,
    }
    state = {"regime": "bull"}

    tid = trade_journal.record_trade(trade, "wf1", state)
    assert tid.startswith("TRD-")

    record = trade_journal.get_trade(tid)
    assert record["symbol"] == "AAPL"
    assert record["status"] == "open"


def test_close_trade(trade_journal):
    trade = {"symbol": "AAPL", "entry_price": 100.0, "quantity": 1, "signal_type": "BUY"}
    tid = trade_journal.record_trade(trade, "wf1", {})

    success = trade_journal.close_trade(tid, 110.0, "target", mae=5, mfe=15)
    assert success is True

    record = trade_journal.get_trade(tid)
    assert record["status"] == "closed"
    assert record["profit_loss"] == 10.0
    assert record["exit_reason"] == "target"


def test_get_trades_by_date(trade_journal):
    trade_journal.record_trade({"symbol": "AAPL"}, "wf1", {})
    trades = trade_journal.get_trades_by_date(datetime(2000, 1, 1))
    assert len(trades) == 1

    # Test with end date
    trades = trade_journal.get_trades_by_date(datetime(2000, 1, 1), datetime(2030, 1, 1))
    assert len(trades) == 1


def test_get_trades_by_strategy(trade_journal):
    trade_journal.record_trade({"symbol": "AAPL", "strategy": "strat1"}, "wf1", {})
    trades = trade_journal.get_trades_by_strategy("strat1")
    assert len(trades) == 1
    assert trades[0]["strategy"] == "strat1"


def test_get_performance_summary(trade_journal):
    # Create a winner
    tid1 = trade_journal.record_trade(
        {"symbol": "A", "entry_price": 100, "quantity": 1, "signal_type": "BUY", "strategy": "s1"},
        "wf1",
        {},
    )
    trade_journal.close_trade(tid1, 110, "target", mae=0, mfe=10)

    # Create a loser
    tid2 = trade_journal.record_trade(
        {"symbol": "B", "entry_price": 100, "quantity": 1, "signal_type": "BUY", "strategy": "s1"},
        "wf1",
        {},
    )
    trade_journal.close_trade(tid2, 90, "stop", mae=10, mfe=0)

    summary = trade_journal.get_performance_summary()
    assert summary["total_trades"] == 2
    assert summary["winners"] == 1
    assert summary["losers"] == 1
    assert summary["total_pnl"] == 0.0


def test_log_decision(trade_journal):
    trade_journal.log_decision("wf1", "agent1", {}, {}, "BUY", 0.9, "reason", 100)
    # Verify by checking DB directly
    log = trade_journal._session.query(DecisionLog).first()
    assert log.agent == "agent1"
    assert log.decision == "BUY"


# --- ExecutionAdapter Tests ---


@pytest.fixture
def mock_dhan():
    with patch("src.execution.adapter.dhanhq") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        yield client


def test_execution_adapter_place_order(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"
        mock_settings.return_value.trading_mode = "paper"

        adapter = ExecutionAdapter()
        mock_dhan.place_order.return_value = {"status": "success", "data": {"orderId": "123"}}

        req = OrderRequest("AAPL", "NSE", OrderSide.BUY, 1)

        import asyncio

        result = asyncio.run(adapter.place_order(req))

        assert result.status == OrderStatus.PLACED
        assert result.order_id == "123"


def test_execution_adapter_place_order_retry(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"

        adapter = ExecutionAdapter(max_retries=2, retry_delay=0.1)
        # Fail then success
        mock_dhan.place_order.side_effect = [
            Exception("Fail"),
            {"status": "success", "data": {"orderId": "123"}},
        ]

        req = OrderRequest("AAPL", "NSE", OrderSide.BUY, 1)

        import asyncio

        result = asyncio.run(adapter.place_order(req))

        assert result.status == OrderStatus.PLACED


def test_execution_adapter_get_order_status(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"

        adapter = ExecutionAdapter()
        mock_dhan.get_order_by_id.return_value = {
            "status": "success",
            "data": {
                "securityId": "AAPL",
                "exchangeSegment": "NSE_EQ",
                "transactionType": "BUY",
                "quantity": 10,
                "orderStatus": "TRADED",
                "filledQty": 10,
                "avgPrice": 150.0,
            },
        }

        import asyncio

        result = asyncio.run(adapter.get_order_status("123"))

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == 10
        assert result.average_price == 150.0


def test_execution_adapter_cancel_order(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"

        adapter = ExecutionAdapter()
        mock_dhan.cancel_order.return_value = {"status": "success"}

        import asyncio

        result = asyncio.run(adapter.cancel_order("123"))

        assert result is True


def test_execution_adapter_get_positions(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"

        adapter = ExecutionAdapter()
        mock_dhan.get_positions.return_value = {"status": "success", "data": [{"symbol": "AAPL"}]}

        import asyncio

        result = asyncio.run(adapter.get_positions())

        assert len(result) == 1
        assert result[0]["symbol"] == "AAPL"


def test_execution_adapter_get_holdings(mock_dhan):
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.dhan_client_id = "id"
        mock_settings.return_value.dhan_access_token.get_secret_value.return_value = "token"

        adapter = ExecutionAdapter()
        mock_dhan.get_holdings.return_value = {"status": "success", "data": [{"symbol": "AAPL"}]}

        import asyncio

        result = asyncio.run(adapter.get_holdings())

        assert len(result) == 1
        assert result[0]["symbol"] == "AAPL"


def test_local_adapter_place_order(tmp_path):
    # Need to init with engine that uses tmp_path to avoid creating file in repo
    engine = LocalPaperEngine(
        state_file=tmp_path / "paper_wallet.json", cost_model=CostModel.zero()
    )
    adapter = LocalExecutionAdapter(_engine=engine)
    req = OrderRequest("AAPL", "NSE", OrderSide.BUY, 10, price=150.0)

    import asyncio

    result = asyncio.run(adapter.place_order(req))

    assert result.status == OrderStatus.FILLED
    assert result.average_price == 150.0


def test_local_adapter_methods(tmp_path):
    engine = LocalPaperEngine(
        state_file=tmp_path / "paper_wallet.json", cost_model=CostModel.zero()
    )
    adapter = LocalExecutionAdapter(_engine=engine)

    import asyncio

    asyncio.run(adapter.place_order(OrderRequest("AAPL", "NSE", OrderSide.BUY, 10, price=150.0)))

    positions = asyncio.run(adapter.get_positions())
    assert len(positions) == 1

    holdings = asyncio.run(adapter.get_holdings())
    assert len(holdings) == 1

    stats = adapter.get_stats()
    assert "balance" in stats

    assert adapter.get_balance() > 0


def test_execute_trades_helper():
    with patch("src.execution.adapter.get_settings") as mock_settings:
        mock_settings.return_value.max_position_size = 10000
        mock_settings.return_value.execution_mode = "local_paper"

        trades = [{"symbol": "AAPL", "entry_price": 100, "signal_type": "BUY"}]
        market_prices = {"AAPL": 100.0}

        import asyncio

        results = asyncio.run(execute_trades(trades, market_prices=market_prices))

        assert len(results) == 1
        assert results[0].status == OrderStatus.FILLED
