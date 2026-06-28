"""
FinOps — LLM cost & token accounting.

Tracks Groq token usage and (paid-tier-equivalent) cost per agent and per IST day,
and exposes a day-budget status so the trading loop can throttle spend. The default
free tier bills $0, but we still account tokens so the same code works on the paid
tier and so the operator can see "this run would cost $X on the paid tier".

This module is pure accounting — no network I/O — so it is safe to call from inside
sync agent nodes. Alerts/notifications live in ``finops.alerts``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from src.config import get_settings
from src.utils.market_time import now_ist

logger = logging.getLogger(__name__)


# Groq list prices in USD per 1M tokens (input, output). These are estimates for
# paid-tier-equivalent cost and are easily overridden via register_pricing(); the
# free tier bills $0 regardless. Unknown models fall back to $0 (token-only tracking).
DEFAULT_GROQ_PRICING: dict[str, tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "llama3-70b-8192": (0.59, 0.79),
    "llama3-8b-8192": (0.05, 0.08),
}
_FALLBACK_PRICING: tuple[float, float] = (0.0, 0.0)


@dataclass
class UsageRecord:
    """A single LLM call's usage."""

    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str  # IST isoformat

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "timestamp": self.timestamp,
        }


@dataclass
class _DailyUsage:
    date: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    by_agent: dict[str, dict[str, float]] = field(default_factory=dict)


class CostTracker:
    """Thread-safe per-day LLM usage accounting."""

    def __init__(self, pricing: dict[str, tuple[float, float]] | None = None) -> None:
        self._lock = threading.Lock()
        self._pricing: dict[str, tuple[float, float]] = dict(pricing or DEFAULT_GROQ_PRICING)
        self._daily = _DailyUsage(date=self._today())

    @staticmethod
    def _today() -> str:
        return now_ist().date().isoformat()

    def register_pricing(self, model: str, input_per_1m: float, output_per_1m: float) -> None:
        """Override pricing for a model (USD per 1M tokens)."""
        with self._lock:
            self._pricing[model] = (input_per_1m, output_per_1m)

    def price_for(self, model: str) -> tuple[float, float]:
        return self._pricing.get(model, _FALLBACK_PRICING)

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self.price_for(model)
        return (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out

    def _roll_if_needed_locked(self) -> None:
        today = self._today()
        if self._daily.date != today:
            logger.info(
                "FinOps daily rollover %s -> %s (closing totals: %d calls, %d tokens, $%.4f)",
                self._daily.date,
                today,
                self._daily.calls,
                self._daily.input_tokens + self._daily.output_tokens,
                self._daily.cost_usd,
            )
            self._daily = _DailyUsage(date=today)

    def record_usage(
        self, agent: str, model: str, input_tokens: int, output_tokens: int
    ) -> UsageRecord:
        """Record one LLM call's token usage and accrue cost."""
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        cost = self.estimate_cost(model, input_tokens, output_tokens)

        with self._lock:
            self._roll_if_needed_locked()
            d = self._daily
            d.input_tokens += input_tokens
            d.output_tokens += output_tokens
            d.cost_usd += cost
            d.calls += 1
            bucket = d.by_agent.setdefault(
                agent, {"input_tokens": 0.0, "output_tokens": 0.0, "cost_usd": 0.0, "calls": 0.0}
            )
            bucket["input_tokens"] += input_tokens
            bucket["output_tokens"] += output_tokens
            bucket["cost_usd"] += cost
            bucket["calls"] += 1

        record = UsageRecord(
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            timestamp=now_ist().isoformat(),
        )
        logger.debug("FinOps usage: %s", record.to_dict())
        return record

    def daily_summary(self) -> dict[str, Any]:
        """Snapshot of today's (IST) usage."""
        with self._lock:
            self._roll_if_needed_locked()
            d = self._daily
            return {
                "date": d.date,
                "calls": d.calls,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "total_tokens": d.input_tokens + d.output_tokens,
                "total_cost_usd": d.cost_usd,
                "by_agent": {k: dict(v) for k, v in d.by_agent.items()},
            }

    @property
    def today_tokens(self) -> int:
        s = self.daily_summary()
        return int(s["total_tokens"])

    @property
    def today_cost_usd(self) -> float:
        return float(self.daily_summary()["total_cost_usd"])

    @property
    def today_calls(self) -> int:
        return int(self.daily_summary()["calls"])

    def budget_status(self) -> dict[str, Any]:
        """
        Compare today's usage against the configured daily budgets.

        A budget of 0 (the default) means "unlimited" and never breaches. Soft breach
        fires at ``finops_budget_soft_pct`` of either budget; hard breach at 100%.
        """
        settings = get_settings()
        token_budget = int(getattr(settings, "daily_token_budget", 0) or 0)
        cost_budget = float(getattr(settings, "daily_cost_budget_usd", 0.0) or 0.0)
        soft_pct = float(getattr(settings, "finops_budget_soft_pct", 0.8) or 0.8)

        summary = self.daily_summary()
        tokens_used = int(summary["total_tokens"])
        cost_used = float(summary["total_cost_usd"])

        token_soft = token_budget > 0 and tokens_used >= token_budget * soft_pct
        token_hard = token_budget > 0 and tokens_used >= token_budget
        cost_soft = cost_budget > 0 and cost_used >= cost_budget * soft_pct
        cost_hard = cost_budget > 0 and cost_used >= cost_budget

        return {
            "date": summary["date"],
            "tokens_used": tokens_used,
            "token_budget": token_budget,
            "cost_used_usd": cost_used,
            "cost_budget_usd": cost_budget,
            "soft_pct": soft_pct,
            "soft_breached": bool(token_soft or cost_soft),
            "hard_breached": bool(token_hard or cost_hard),
        }

    def is_over_hard_budget(self) -> bool:
        return bool(self.budget_status()["hard_breached"])


# --- Module-level singleton + LLM-response helper -------------------------------

_tracker: CostTracker | None = None
_tracker_lock = threading.Lock()


def get_cost_tracker() -> CostTracker:
    """Get or create the shared CostTracker."""
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = CostTracker()
    return _tracker


def reset_cost_tracker() -> None:
    """Reset the shared CostTracker (test isolation)."""
    global _tracker
    with _tracker_lock:
        _tracker = None


def _extract_usage(response: Any) -> tuple[int, int] | None:
    """Pull (input_tokens, output_tokens) out of a LangChain chat response."""
    usage_metadata = getattr(response, "usage_metadata", None)
    if isinstance(usage_metadata, dict) and usage_metadata:
        return (
            int(usage_metadata.get("input_tokens", 0) or 0),
            int(usage_metadata.get("output_tokens", 0) or 0),
        )
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        token_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}
        if isinstance(token_usage, dict) and token_usage:
            return (
                int(token_usage.get("prompt_tokens", 0) or 0),
                int(token_usage.get("completion_tokens", 0) or 0),
            )
    return None


def _extract_model(response: Any) -> str | None:
    response_metadata = getattr(response, "response_metadata", None)
    if isinstance(response_metadata, dict):
        model = response_metadata.get("model_name") or response_metadata.get("model")
        if model:
            return str(model)
    return None


def record_llm_response(agent: str, response: Any, model: str | None = None) -> UsageRecord | None:
    """
    Record token usage from a LangChain chat response. Never raises — FinOps
    accounting must never crash an agent node.
    """
    try:
        settings = get_settings()
        if not getattr(settings, "finops_enabled", True):
            return None
        usage = _extract_usage(response)
        if usage is None:
            logger.debug("FinOps: no usage metadata on response from %s", agent)
            return None
        input_tokens, output_tokens = usage
        resolved_model = model or _extract_model(response) or settings.groq_model_primary
        return get_cost_tracker().record_usage(agent, resolved_model, input_tokens, output_tokens)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("FinOps record_llm_response failed (non-fatal): %s", exc)
        return None
