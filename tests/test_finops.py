"""
Tests for the FinOps layer: LLM cost/token accounting, budgets, and alerts.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from src.finops.alerts import AlertManager, get_alert_manager, reset_alert_manager
from src.finops.cost_tracker import (
    CostTracker,
    _extract_model,
    _extract_usage,
    get_cost_tracker,
    record_llm_response,
    reset_cost_tracker,
)


class _FakeResponse:
    """Stand-in for a LangChain chat response."""

    def __init__(self, usage_metadata=None, response_metadata=None):
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata
        if response_metadata is not None:
            self.response_metadata = response_metadata


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


def test_estimate_cost_known_model():
    t = CostTracker()
    # llama-3.3-70b-versatile = (0.59, 0.79) USD / 1M tokens
    cost = t.estimate_cost("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert round(cost, 6) == round(0.59 + 0.79, 6)


def test_estimate_cost_unknown_model_is_free():
    t = CostTracker()
    assert t.estimate_cost("some-unknown-model", 5000, 5000) == 0.0


def test_record_usage_accumulates_and_aggregates_by_agent():
    t = CostTracker()
    t.record_usage("market_regime", "llama-3.3-70b-versatile", 1000, 500)
    t.record_usage("market_regime", "llama-3.3-70b-versatile", 2000, 1000)
    t.record_usage("news_analyst", "llama-3.1-8b-instant", 400, 100)

    summary = t.daily_summary()
    assert summary["calls"] == 3
    assert summary["input_tokens"] == 3400
    assert summary["output_tokens"] == 1600
    assert summary["total_tokens"] == 5000
    assert summary["by_agent"]["market_regime"]["calls"] == 2
    assert summary["by_agent"]["news_analyst"]["input_tokens"] == 400
    assert summary["total_cost_usd"] > 0


def test_register_pricing_override():
    t = CostTracker()
    t.register_pricing("custom-model", 10.0, 20.0)
    assert t.estimate_cost("custom-model", 1_000_000, 0) == 10.0


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def _settings(token_budget=0, cost_budget=0.0, soft=0.8):
    return MagicMock(
        daily_token_budget=token_budget,
        daily_cost_budget_usd=cost_budget,
        finops_budget_soft_pct=soft,
    )


def test_budget_unlimited_never_breaches():
    t = CostTracker()
    t.record_usage("a", "llama-3.3-70b-versatile", 10_000, 10_000)
    with patch("src.finops.cost_tracker.get_settings", return_value=_settings()):
        status = t.budget_status()
    assert status["soft_breached"] is False
    assert status["hard_breached"] is False


def test_token_budget_hard_breach():
    t = CostTracker()
    t.record_usage("a", "llama-3.3-70b-versatile", 1000, 0)
    with patch("src.finops.cost_tracker.get_settings", return_value=_settings(token_budget=1000)):
        status = t.budget_status()
        assert status["hard_breached"] is True
        assert status["soft_breached"] is True
        assert t.is_over_hard_budget() is True


def test_token_budget_soft_only():
    t = CostTracker()
    t.record_usage("a", "llama-3.3-70b-versatile", 1000, 0)
    with patch("src.finops.cost_tracker.get_settings", return_value=_settings(token_budget=1100)):
        status = t.budget_status()
    assert status["soft_breached"] is True  # 1000 >= 1100 * 0.8 (880)
    assert status["hard_breached"] is False  # 1000 < 1100


def test_cost_budget_hard_breach():
    t = CostTracker()
    # 1M input @ $0.59 = $0.59 spend
    t.record_usage("a", "llama-3.3-70b-versatile", 1_000_000, 0)
    with patch("src.finops.cost_tracker.get_settings", return_value=_settings(cost_budget=0.5)):
        status = t.budget_status()
    assert status["hard_breached"] is True


# ---------------------------------------------------------------------------
# Usage extraction from LangChain responses
# ---------------------------------------------------------------------------


def test_extract_usage_from_usage_metadata():
    resp = _FakeResponse(usage_metadata={"input_tokens": 120, "output_tokens": 30})
    assert _extract_usage(resp) == (120, 30)


def test_extract_usage_from_response_metadata_token_usage():
    resp = _FakeResponse(
        response_metadata={"token_usage": {"prompt_tokens": 200, "completion_tokens": 40}}
    )
    assert _extract_usage(resp) == (200, 40)


def test_extract_usage_none_when_absent():
    assert _extract_usage(_FakeResponse()) is None


def test_extract_model_from_response_metadata():
    resp = _FakeResponse(response_metadata={"model_name": "llama-3.1-8b-instant"})
    assert _extract_model(resp) == "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# record_llm_response (the agent-side hook)
# ---------------------------------------------------------------------------


def test_record_llm_response_records_to_singleton():
    reset_cost_tracker()
    resp = _FakeResponse(usage_metadata={"input_tokens": 1000, "output_tokens": 500})
    rec = record_llm_response("market_regime", resp, model="llama-3.3-70b-versatile")
    assert rec is not None
    assert rec.input_tokens == 1000
    assert rec.output_tokens == 500
    assert get_cost_tracker().today_tokens == 1500


def test_record_llm_response_disabled_returns_none():
    reset_cost_tracker()
    resp = _FakeResponse(usage_metadata={"input_tokens": 1000, "output_tokens": 500})
    with patch(
        "src.finops.cost_tracker.get_settings",
        return_value=MagicMock(finops_enabled=False),
    ):
        rec = record_llm_response("market_regime", resp)
    assert rec is None


def test_record_llm_response_never_raises_on_bad_response():
    reset_cost_tracker()
    # An object with no usage metadata at all must not raise.
    assert record_llm_response("x", object()) is None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


async def test_alert_dedup_and_telegram_send():
    reset_alert_manager()
    manager = get_alert_manager()

    notifier = MagicMock()
    notifier.enabled = True
    notifier.send_message = AsyncMock(return_value=True)

    with patch("src.notifications.telegram.get_notifier", return_value=notifier):
        first = await manager.alert("budget", "over budget")
        second = await manager.alert("budget", "over budget")  # same key, same day

    assert first is True
    assert second is False  # de-duplicated
    notifier.send_message.assert_awaited_once()


async def test_alert_logs_when_telegram_disabled():
    reset_alert_manager()
    manager = get_alert_manager()
    notifier = MagicMock()
    notifier.enabled = False

    with patch("src.notifications.telegram.get_notifier", return_value=notifier):
        dispatched = await manager.alert("staleness", "stale data")

    assert dispatched is True  # still logged even with Telegram off


async def test_alert_once_per_day_false_allows_repeat():
    manager = AlertManager()
    notifier = MagicMock()
    notifier.enabled = False
    with patch("src.notifications.telegram.get_notifier", return_value=notifier):
        a = await manager.alert("k", "m", once_per_day=False)
        b = await manager.alert("k", "m", once_per_day=False)
    assert a is True and b is True
