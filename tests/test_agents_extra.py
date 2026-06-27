import pytest
from unittest.mock import MagicMock, patch
from src.agents.state import create_initial_state, MarketRegime
from src.agents.market_regime import market_regime_node, _build_regime_context, _parse_regime_response
from src.agents.risk_compliance import risk_compliance_node, RiskLimits, check_kill_switch
from src.agents.signal_validation import signal_validation_node, _build_validation_context, _parse_validation_response
from src.agents.strategy_selection import strategy_selection_node, _build_strategy_context, _parse_strategy_response

# Mock settings
with patch("src.config.get_settings") as mock_get_settings:
    mock_settings = MagicMock()
    mock_settings.groq_api_key.get_secret_value.return_value = "token"
    mock_settings.groq_model_primary = "llama"
    mock_get_settings.return_value = mock_settings

# --- Market Regime Tests ---

def test_build_regime_context():
    indicators = {"A": {"trend": {"adx": 30}}}
    market_data = {"A": {"change_percent": 1.0}}
    lessons = [{"severity": "high", "description": "mistake"}]

    context = _build_regime_context(indicators, market_data, lessons)
    assert "ADX" in context
    assert "mistake" in context

def test_parse_regime_response():
    # Valid
    content = '{"regime": "trending_up", "confidence": 0.8, "reasoning": "trend"}'
    res = _parse_regime_response(content)
    assert res["regime"] == "trending_up"

    # Invalid regime
    content = '{"regime": "invalid", "confidence": 0.8}'
    res = _parse_regime_response(content)
    assert res["regime"] == MarketRegime.UNKNOWN.value

    # Invalid JSON
    res = _parse_regime_response("invalid json")
    assert res["regime"] == MarketRegime.UNKNOWN.value

@patch("src.agents.market_regime.ChatGroq")
@patch("src.agents.market_regime.get_settings")
def test_market_regime_node(mock_settings, mock_llm_cls):
    mock_settings.return_value.groq_api_key.get_secret_value.return_value = "token"
    mock_llm = MagicMock()
    mock_llm.invoke.return_value.content = '{"regime": "ranging", "confidence": 0.6, "reasoning": "flat"}'
    mock_llm_cls.return_value = mock_llm

    state = create_initial_state()
    result = market_regime_node(state)

    assert result["regime"] == "ranging"
    assert result["regime_confidence"] == 0.6

@patch("src.agents.market_regime.ChatGroq")
def test_market_regime_node_error(mock_llm_cls):
    mock_llm_cls.side_effect = Exception("API Error")

    state = create_initial_state()
    state["market_data"] = {"A": {"change_percent": 1.0}} # Positive -> trending_up fallback

    with patch("src.agents.market_regime.get_settings"):
        result = market_regime_node(state)

    assert result["regime"] == "trending_up"
    assert "fallback" in result["regime_reasoning"].lower()


# --- Risk Compliance Tests ---

def test_risk_compliance_node():
    state = create_initial_state()
    state["validated_signals"] = [
        {"symbol": "A", "confidence": 0.8, "risk_reward_ratio": 2.0}
    ]

    # Mock IST clock to be within trading hours
    with patch("src.agents.risk_compliance.now_ist") as mock_now:
        mock_now.return_value.strftime.return_value = "12:00"

        with patch("src.agents.risk_compliance.RiskLimits.from_settings") as mock_limits:
            limits = RiskLimits() # Defaults
            mock_limits.return_value = limits

            result = risk_compliance_node(state)

            assert len(result["approved_trades"]) == 1
            assert result["approved_trades"][0]["risk_result"]["approved"] is True

def test_risk_compliance_blocking():
    state = create_initial_state()
    # Daily trade limit exceeded
    state["daily_stats"]["trades_count"] = 100
    state["validated_signals"] = [{"symbol": "A"}]

    with patch("src.agents.risk_compliance.RiskLimits.from_settings") as mock_limits:
        limits = RiskLimits(max_daily_trades=50)
        mock_limits.return_value = limits

        result = risk_compliance_node(state)

        assert len(result["approved_trades"]) == 0
        assert len(result["risk_rejected"]) == 1

def test_check_kill_switch():
    state = create_initial_state()
    limits = RiskLimits(max_daily_loss=1000)

    # Safe
    state["daily_stats"]["profit_loss"] = -500
    assert check_kill_switch(state, limits) is False

    # Breached
    state["daily_stats"]["profit_loss"] = -1500
    assert check_kill_switch(state, limits) is True


# --- Signal Validation Tests ---

def test_build_validation_context():
    signals = [{"symbol": "A", "confidence": 0.8}]
    context = _build_validation_context(signals, "bull", 0.9, ["mom"], [])
    assert "A" in context
    assert "bull" in context

def test_parse_validation_response():
    signals = [{"signal_id": "1"}, {"signal_id": "2"}]
    content = '{"validations": [{"signal_id": "1", "decision": "approve"}, {"signal_id": "2", "decision": "reject"}]}'

    res = _parse_validation_response(content, signals)
    assert len(res["validated"]) == 1
    assert res["validated"][0]["signal_id"] == "1"
    assert len(res["rejected"]) == 1

@patch("src.agents.signal_validation.ChatGroq")
@patch("src.agents.signal_validation.get_settings")
def test_signal_validation_node(mock_settings, mock_llm_cls):
    mock_settings.return_value.groq_api_key.get_secret_value.return_value = "token"
    mock_llm = MagicMock()
    mock_llm.invoke.return_value.content = '{"validations": [{"signal_id": "1", "decision": "approve"}]}'
    mock_llm_cls.return_value = mock_llm

    state = create_initial_state()
    state["signals"] = [{"signal_id": "1"}]

    result = signal_validation_node(state)
    assert len(result["validated_signals"]) == 1


# --- Strategy Selection Tests ---

def test_build_strategy_context():
    context = _build_strategy_context("bull", 0.9, [], {})
    assert "bull" in context

def test_parse_strategy_response():
    content = '{"active_strategies": ["momentum"], "reasoning": "trend"}'
    res = _parse_strategy_response(content)
    assert res["active_strategies"] == ["momentum"]

    # Invalid strategy filtered
    content = '{"active_strategies": ["invalid"], "reasoning": "none"}'
    res = _parse_strategy_response(content)
    # Defaults to trend_following if list empty
    assert "trend_following" in res["active_strategies"]

@patch("src.agents.strategy_selection.ChatGroq")
@patch("src.agents.strategy_selection.get_settings")
def test_strategy_selection_node(mock_settings, mock_llm_cls):
    mock_settings.return_value.groq_api_key.get_secret_value.return_value = "token"
    mock_llm = MagicMock()
    mock_llm.invoke.return_value.content = '{"active_strategies": ["breakout"], "reasoning": "vol"}'
    mock_llm_cls.return_value = mock_llm

    state = create_initial_state()
    result = strategy_selection_node(state)

    assert result["active_strategies"] == ["breakout"]
