"""
Tests for PR-0 correctness quick-wins:

- C2: support-agent state contracts (news_sentiment / market_mood dicts, prediction
  sourcing) no longer crash the regime/validation context builders and actually
  reach the LLM context.
- C3: market-hours and risk trading-hours evaluated in IST regardless of host tz.
- C1: kill-switch predicate covers daily-loss and drawdown breaches.
- M10: cross-field config warnings are surfaced on the Settings instance.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pandas as pd

from src.agents.market_regime import _build_regime_context_enriched
from src.agents.prediction import prediction_node
from src.agents.risk_compliance import RiskLimits, check_kill_switch
from src.agents.signal_validation import _build_validation_context_enriched
from src.agents.state import create_initial_state
from src.config.settings import Settings
from src.utils.market_time import IST, is_market_hours, now_ist

# ---------------------------------------------------------------------------
# C3 — IST market hours (host-timezone independent)
# ---------------------------------------------------------------------------

def test_now_ist_is_fixed_utc_plus_530():
    """now_ist() must always be UTC+05:30, independent of the host timezone."""
    assert now_ist().utcoffset() == timedelta(hours=5, minutes=30)


def test_is_market_hours_weekday_open():
    # 2024-01-01 is a Monday.
    assert is_market_hours(datetime(2024, 1, 1, 10, 0, tzinfo=IST)) is True


def test_is_market_hours_weekend_closed():
    # 2024-01-06 is a Saturday.
    assert is_market_hours(datetime(2024, 1, 6, 10, 0, tzinfo=IST)) is False


def test_is_market_hours_before_open_and_after_close():
    assert is_market_hours(datetime(2024, 1, 1, 8, 0, tzinfo=IST)) is False
    assert is_market_hours(datetime(2024, 1, 1, 16, 0, tzinfo=IST)) is False


def test_is_market_hours_uses_ist_not_host_clock():
    """
    10:00 IST == 04:30 UTC. A correct implementation that converts to IST stays
    open; a buggy one comparing UTC wall-clock (04:30) against IST open (09:15)
    would report closed.
    """
    instant = datetime(2024, 1, 1, 4, 30, tzinfo=UTC)
    assert is_market_hours(instant.astimezone(IST)) is True


# ---------------------------------------------------------------------------
# C2 — support-agent state contracts
# ---------------------------------------------------------------------------

def test_regime_context_does_not_crash_on_empty_initial_state():
    """
    Regression: create_initial_state() sets news_sentiment={} and market_mood={};
    the old code formatted those dicts with :.2f and raised
    'unsupported format string passed to dict.__format__'.
    """
    state = create_initial_state()
    # Must not raise.
    context = _build_regime_context_enriched({}, {}, [], state)
    assert isinstance(context, str)


def test_validation_context_does_not_crash_on_empty_initial_state():
    state = create_initial_state()
    context = _build_validation_context_enriched([], "ranging", 0.5, [], [], state)
    assert isinstance(context, str)


def test_regime_context_includes_canonical_enrichment():
    state = create_initial_state()
    state["news_sentiment"] = {"avg_sentiment": 0.42}
    state["news_headlines"] = [{"title": "Banks rally", "sentiment": "positive"}]
    state["market_mood"] = {"mood_index": 72, "mood_label": "greed", "news_score": 0.42}
    state["prediction_signals"] = [{"symbol": "RELIANCE", "direction": "up", "confidence": 0.7}]

    context = _build_regime_context_enriched({}, {}, [], state)

    assert "News Sentiment" in context
    assert "0.42" in context
    assert "Market Mood Index" in context
    assert "greed" in context
    assert "ML Prediction Consensus" in context
    assert "RELIANCE" in context


def test_validation_context_includes_canonical_enrichment():
    signals = [{"symbol": "RELIANCE", "signal_id": "1"}]
    state = create_initial_state()
    state["news_sentiment"] = {"avg_sentiment": -0.7}
    state["market_mood"] = {"mood_index": 25, "mood_label": "fear"}
    state["prediction_signals"] = [{"symbol": "RELIANCE", "direction": "down", "confidence": 0.8}]

    context = _build_validation_context_enriched(signals, "volatile", 0.6, [], [], state)

    assert "News Sentiment" in context
    assert "Market Mood" in context
    assert "fear" in context
    assert "ML Prediction Consensus" in context


def test_regime_context_tolerates_legacy_float_news_sentiment():
    """Defensive: a stray float news_sentiment must not raise."""
    state = create_initial_state()
    state["news_sentiment"] = 0.5  # wrong (legacy) type
    context = _build_regime_context_enriched({}, {}, [], state)
    assert isinstance(context, str)


# ---------------------------------------------------------------------------
# C2 — prediction_node sources from raw signals (not the always-empty
#       validated_signals), and de-duplicates symbols.
# ---------------------------------------------------------------------------

def test_prediction_node_sources_from_signals_and_dedupes():
    state = create_initial_state()
    state["signals"] = [
        {"symbol": "RELIANCE"},
        {"symbol": "RELIANCE"},  # duplicate -> deduped
        {"symbol": "TCS"},
    ]
    state["validated_signals"] = []  # empty at support-agent stage

    with patch("src.market.yfinance_feed.YFinanceFeed") as mock_feed_cls, patch(
        "src.agents.prediction.PredictionAgent.predict"
    ) as mock_predict:
        mock_feed_cls.return_value.get_historical.return_value = pd.DataFrame(
            {"Close": [1.0, 2.0, 3.0]}
        )
        mock_predict.return_value.to_dict.return_value = {"symbol": "X", "direction": "up"}

        result = prediction_node(state)

    # Two distinct symbols -> two predictions (proves sourcing from `signals`).
    assert len(result["prediction_signals"]) == 2


def test_prediction_node_ignores_validated_signals():
    """The fix means validated_signals (empty here anyway) is no longer the source."""
    state = create_initial_state()
    state["signals"] = []
    state["validated_signals"] = [{"symbol": "RELIANCE"}]

    with patch("src.market.yfinance_feed.YFinanceFeed") as mock_feed_cls:
        mock_feed_cls.return_value.get_historical.return_value = pd.DataFrame({"Close": [1.0]})
        result = prediction_node(state)

    assert result["prediction_signals"] == []


# ---------------------------------------------------------------------------
# C1 — kill-switch predicate (the gate added to the live loop relies on this)
# ---------------------------------------------------------------------------

def test_kill_switch_on_daily_loss():
    state = create_initial_state()
    limits = RiskLimits(max_daily_loss=10000.0, max_drawdown_pct=99.0)
    state["daily_stats"]["profit_loss"] = -10000.0
    assert check_kill_switch(state, limits) is True


def test_kill_switch_on_drawdown():
    state = create_initial_state()
    limits = RiskLimits(max_daily_loss=10_000_000.0, max_drawdown_pct=5.0)
    state["portfolio"] = {"capital": 1_000_000.0}
    state["daily_stats"]["max_drawdown"] = 60_000.0  # 6% > 5%
    assert check_kill_switch(state, limits) is True


def test_kill_switch_safe_when_within_limits():
    state = create_initial_state()
    limits = RiskLimits(max_daily_loss=10000.0, max_drawdown_pct=5.0)
    state["portfolio"] = {"capital": 1_000_000.0}
    state["daily_stats"]["profit_loss"] = -500.0
    state["daily_stats"]["max_drawdown"] = 1000.0  # 0.1%
    assert check_kill_switch(state, limits) is False


# ---------------------------------------------------------------------------
# M10 — config warnings surfaced on the Settings instance
# ---------------------------------------------------------------------------

def test_config_warnings_flag_inconsistent_risk_params():
    s = Settings(
        groq_api_key="x",
        langsmith_api_key="y",
        risk_per_trade=0.5,  # exceeds max_position_pct -> warning
        max_position_pct=0.1,
    )
    assert any("risk_per_trade" in w for w in s.config_warnings)


def test_config_warnings_clean_defaults():
    s = Settings(groq_api_key="x", langsmith_api_key="y")
    assert not any("risk_per_trade" in w for w in s.config_warnings)
