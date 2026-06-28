import pytest

from src.dashboard.cli import (
    TradingDashboard,
    TradingStats,
    create_account_panel,
    create_activity_panel,
    create_agent_panel,
    create_dashboard_layout,
    create_decision_panel,
    create_header,
    create_market_overview,
    create_positions_panel,
    create_regime_panel,
    create_status_panel,
    create_trades_panel,
)

# --- TradingStats Tests ---


@pytest.fixture
def stats():
    return TradingStats()


def test_trading_stats_init(stats):
    assert stats.starting_balance == 1000000.0
    assert stats.total_trades == 0
    assert stats.win_rate == 0.0


def test_trading_stats_pnl(stats):
    stats.realized_pnl = 100
    stats.unrealized_pnl = 50
    assert stats.total_pnl == 150
    assert stats.pnl_percent == 0.015


def test_trading_stats_log_activity(stats):
    stats.log_activity("Test message", "INFO")
    assert len(stats.activity_log) == 1
    assert stats.activity_log[0]["message"] == "Test message"

    # Test capping
    for i in range(20):
        stats.log_activity(f"Msg {i}")
    assert len(stats.activity_log) == 12  # Cap size


# --- TradingDashboard Tests ---


@pytest.fixture
def dashboard():
    return TradingDashboard()


def test_dashboard_start(dashboard):
    dashboard.start(balance=500000.0, mode="live", data_source="dhan")
    assert dashboard.stats.starting_balance == 500000.0
    assert dashboard.stats.trading_mode == "live"
    assert dashboard.running is True
    assert len(dashboard.stats.activity_log) == 3


def test_dashboard_update_regime(dashboard):
    dashboard.update_regime("bull", 0.9, ["strat1"])
    assert dashboard.stats.current_regime == "bull"
    assert dashboard.stats.regime_confidence == 0.9
    assert dashboard.stats.active_strategies == ["strat1"]


def test_dashboard_update_market_data(dashboard):
    quotes = {"A": 100}
    dashboard.update_market_data(quotes)
    assert dashboard.stats.market_quotes == quotes


def test_dashboard_set_current_signal(dashboard):
    dashboard.set_current_signal("BUY", "AAPL", "strat1", 0.8)
    assert dashboard.stats.current_signal["symbol"] == "AAPL"


def test_dashboard_set_decision_reason(dashboard):
    dashboard.set_decision_reason("Reason")
    assert dashboard.stats.last_decision_reason == "Reason"


def test_dashboard_log_signal(dashboard):
    dashboard.log_signal("AAPL", "BUY", "strat1", True)
    assert dashboard.stats.signals_generated == 1
    assert dashboard.stats.signals_validated == 1

    dashboard.log_signal("AAPL", "BUY", "strat1", False)
    assert dashboard.stats.signals_generated == 2
    assert dashboard.stats.signals_rejected == 1


def test_dashboard_log_trade(dashboard):
    dashboard.log_trade("AAPL", "BUY", 10, 100, True)
    assert dashboard.stats.trades_approved == 1

    dashboard.log_trade("AAPL", "BUY", 10, 100, False)
    assert dashboard.stats.trades_risk_rejected == 1


def test_dashboard_add_position(dashboard):
    dashboard.add_position("AAPL", "BUY", 10, 100)
    assert len(dashboard.stats.open_positions) == 1
    assert dashboard.stats.open_positions[0]["symbol"] == "AAPL"


def test_dashboard_close_trade(dashboard):
    dashboard.start()
    dashboard.close_trade(100.0)
    assert dashboard.stats.total_trades == 1
    assert dashboard.stats.winning_trades == 1
    assert dashboard.stats.realized_pnl == 100.0
    assert dashboard.stats.current_balance == 1000100.0

    dashboard.close_trade(-50.0)
    assert dashboard.stats.losing_trades == 1
    assert dashboard.stats.realized_pnl == 50.0


def test_dashboard_increment_cycle(dashboard):
    dashboard.increment_cycle()
    assert dashboard.stats.cycles_run == 1


# --- Panel Creation Tests ---
# These tests verify that panel creation functions run without error.
# Checking the visual output content is less critical than ensuring they don't crash.


def test_create_panels(stats):
    # Populate stats with some data
    stats.current_balance = 1100000
    stats.total_trades = 10
    stats.winning_trades = 6
    stats.losing_trades = 4
    stats.current_regime = "trending_up"
    stats.regime_confidence = 0.8
    stats.active_strategies = ["momentum"]
    stats.market_quotes = {"AAPL": {"last_price": 150, "change_percent": 1.5}}
    stats.current_signal = {
        "signal_type": "BUY",
        "symbol": "AAPL",
        "strategy": "momentum",
        "confidence": 0.9,
    }
    stats.last_decision_reason = "Reason"
    stats.open_positions = [{"symbol": "AAPL", "side": "BUY", "qty": 10, "entry": 140, "pnl": 100}]
    stats.log_activity("Test")

    # Check panels
    assert create_header(stats) is not None
    assert create_account_panel(stats) is not None
    assert create_trades_panel(stats) is not None
    assert create_regime_panel(stats) is not None
    assert create_market_overview(stats) is not None
    assert create_decision_panel(stats) is not None
    assert create_agent_panel(stats) is not None
    assert create_positions_panel(stats) is not None
    assert create_activity_panel(stats) is not None

    # Check layout
    layout = create_dashboard_layout(stats)
    assert layout is not None


def test_dashboard_render(dashboard):
    dashboard.start()
    layout = dashboard.render()
    assert layout is not None


def test_create_status_panel_with_finops_and_goal(stats):
    # Status panel renders FinOps spend + profit-goal pace without crashing.
    stats.llm_calls = 12
    stats.llm_tokens = 34567
    stats.llm_cost_usd = 0.0123
    stats.goal_enabled = True
    stats.goal_feasible = True
    stats.goal_on_pace = False
    stats.goal_mtd_pnl = 5000.0
    stats.goal_expected_to_date = 8000.0
    assert create_status_panel(stats) is not None


def test_create_status_panel_goal_disabled(stats):
    # Goal section is simply omitted when disabled.
    stats.goal_enabled = False
    assert create_status_panel(stats) is not None
