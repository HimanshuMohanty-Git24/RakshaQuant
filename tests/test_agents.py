"""
Tests for the agent modules.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.risk_compliance import RiskLimits, check_kill_switch, risk_compliance_node
from src.agents.state import MarketRegime, create_initial_state


class TestTradingState:
    """Tests for the trading state module."""

    def test_create_initial_state(self):
        """Test initial state creation."""
        state = create_initial_state()

        assert state is not None
        assert state["regime"] == MarketRegime.UNKNOWN.value
        assert state["regime_confidence"] == 0.0
        assert state["signals"] == []
        assert state["validated_signals"] == []
        assert state["approved_trades"] == []
        assert state["memory_lessons"] == []
        assert "workflow_id" in state

    def test_create_initial_state_with_workflow_id(self):
        """Test initial state with custom workflow ID."""
        state = create_initial_state(workflow_id="TEST-123")

        assert state["workflow_id"] == "TEST-123"


class TestRiskCompliance:
    """Tests for the risk compliance agent."""

    def test_risk_limits_from_settings(self):
        """Test risk limits creation from settings."""
        with patch("src.agents.risk_compliance.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                max_daily_trades=50,
                daily_loss_limit=10000.0,
            )

            limits = RiskLimits.from_settings()

            assert limits.max_daily_trades == 50
            assert limits.max_daily_loss == 10000.0

    def test_risk_compliance_no_signals(self):
        """Test risk compliance with no validated signals."""
        state = create_initial_state()
        state["validated_signals"] = []

        result = risk_compliance_node(state)

        assert result["approved_trades"] == []
        assert result["risk_rejected"] == []
        assert result["trades_to_execute"] == []

    def test_risk_compliance_rejects_exceeded_daily_trades(self):
        """Test risk compliance rejects when daily trade limit exceeded."""
        state = create_initial_state()
        state["validated_signals"] = [
            {
                "signal_id": "SIG-001",
                "symbol": "RELIANCE",
                "signal_type": "BUY",
                "entry_price": 2500.0,
                "stop_loss": 2450.0,
                "position_size_pct": 5.0,
                "risk_reward_ratio": 2.0,
                "confidence": 0.7,
            }
        ]
        state["daily_stats"] = {"trades_count": 100, "profit_loss": 0, "max_drawdown": 0}
        state["portfolio"] = {"capital": 1000000, "positions": []}

        with patch("src.agents.risk_compliance.RiskLimits.from_settings") as mock_limits:
            mock_limits.return_value = RiskLimits(max_daily_trades=50)
            result = risk_compliance_node(state)

        # Should reject because trades_count > max_daily_trades
        assert len(result["risk_rejected"]) == 1
        assert len(result["approved_trades"]) == 0

    def test_kill_switch_triggered_on_loss(self):
        """Test kill switch triggers on daily loss limit."""
        state = create_initial_state()
        state["daily_stats"] = {"profit_loss": -15000, "max_drawdown": 0}
        state["portfolio"] = {"capital": 1000000, "positions": []}

        limits = RiskLimits(max_daily_loss=10000)

        result = check_kill_switch(state, limits)

        assert result is True

    def test_kill_switch_not_triggered(self):
        """Test kill switch doesn't trigger within limits."""
        state = create_initial_state()
        state["daily_stats"] = {"profit_loss": -5000, "max_drawdown": 0}
        state["portfolio"] = {"capital": 1000000, "positions": []}

        limits = RiskLimits(max_daily_loss=10000)

        result = check_kill_switch(state, limits)

        assert result is False


class TestMarketRegimeAgent:
    """Tests for the market regime agent."""

    def test_regime_context_building(self):
        """Test that regime context is properly built."""
        from src.agents.market_regime import _build_regime_context

        indicators = {
            "RELIANCE": {
                "trend": {"adx": 30, "plus_di": 25, "minus_di": 15},
                "momentum": {"rsi": 55},
                "moving_averages": {"sma": {20: 2450, 50: 2400}},
                "volatility": {"atr": 25, "bb_percent": 0.7},
            }
        }
        market_data = {"RELIANCE": {"close": 2500, "change_percent": 1.5}}
        lessons = [
            {
                "severity": "high",
                "description": "Avoid momentum in ranging markets",
            }
        ]

        context = _build_regime_context(indicators, market_data, lessons)

        assert "RELIANCE" in context
        assert "ADX" in context
        assert "momentum" in context.lower() or "RSI" in context


class TestSignalValidation:
    """Tests for signal validation."""

    def test_validation_with_no_signals(self):
        """Test validation returns empty when no signals."""
        from src.agents.signal_validation import signal_validation_node

        state = create_initial_state()
        state["signals"] = []

        result = signal_validation_node(state)

        assert result["validated_signals"] == []
        assert result["rejected_signals"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
