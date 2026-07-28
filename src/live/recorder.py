"""
Snapshot + trace serialisation for the live session.

Two jobs:

1. ``snapshot_from_stats`` turns the in-process :class:`TradingStats` dataclass (the single
   source of dashboard truth the CLI already builds) into a JSON-safe dict for the web UI.
   The CLI ``rich`` renderer and this serialiser read from the *same* object, so the two
   front ends can never drift.
2. :class:`CycleRecorder` reconstructs a trade cycle as an observability **trace** whose
   **spans** are the LangGraph pipeline nodes (support_agents → market_regime →
   strategy_selection → signal_validation → risk_compliance). Per-span token/cost come from
   the FinOps ``by_agent`` accounting; per-span decision/confidence/reasoning come from the
   final agent state. ``risk_compliance`` is deterministic, so it honestly carries latency
   but zero tokens.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.utils.market_time import is_market_hours, now_ist

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.dashboard.cli import TradingStats


# Ordered pipeline nodes → span display metadata. ``kind`` drives the UI's per-span
# treatment (LLM spans show tokens+cost; the deterministic risk engine shows latency only).
_PIPELINE_SPANS: list[tuple[str, str, str]] = [
    ("support_agents", "Support Agents", "support"),
    ("market_regime", "Market Regime", "llm"),
    ("strategy_selection", "Strategy Selection", "llm"),
    ("signal_validation", "Signal Validation", "llm"),
    ("risk_compliance", "Risk Compliance", "deterministic"),
]

# FinOps records usage under the agent's own key; map graph node → FinOps agent key.
_FINOPS_AGENT_KEY = {
    "market_regime": "market_regime",
    "strategy_selection": "strategy_selection",
    "signal_validation": "signal_validation",
    "support_agents": "support_agents",
}


def env_badge(effective_mode: str) -> str:
    """Map the resolved execution mode to a UI environment badge (PAPER/SHADOW/LIVE)."""
    if effective_mode == "live":
        return "LIVE"
    if effective_mode in ("shadow", "dhan_paper"):
        return "SHADOW"
    return "PAPER"


def snapshot_from_stats(
    stats: TradingStats,
    *,
    run_status: str,
    effective_mode: str,
) -> dict[str, Any]:
    """Serialise the live dashboard state into the JSON contract the web UI consumes."""
    quotes = [
        {
            "symbol": symbol,
            "ltp": float(q.get("last_price", 0) or 0),
            "changePercent": float(q.get("change_percent", 0) or 0),
        }
        for symbol, q in sorted(
            stats.market_quotes.items(),
            key=lambda kv: kv[1].get("change_percent", 0),
            reverse=True,
        )
    ]

    return {
        "ts": now_ist().isoformat(),
        "run": {
            "status": run_status,
            "cycle": stats.cycles_run,
            "mode": stats.trading_mode,
            "dataSource": stats.data_source,
            "executionMode": effective_mode,
            "env": env_badge(effective_mode),
            "marketOpen": is_market_hours(),
            "sessionStartTs": stats.session_start.isoformat(),
        },
        "account": {
            "startingBalance": stats.starting_balance,
            "currentBalance": stats.current_balance,
            "realizedPnl": stats.realized_pnl,
            "unrealizedPnl": stats.unrealized_pnl,
            "totalPnl": stats.total_pnl,
            "pnlPercent": stats.pnl_percent,
            "bestTrade": stats.best_trade,
            "worstTrade": stats.worst_trade,
        },
        "trades": {
            "total": stats.total_trades,
            "winners": stats.winning_trades,
            "losers": stats.losing_trades,
            "winRate": stats.win_rate,
        },
        "agents": {
            "cyclesRun": stats.cycles_run,
            "signalsGenerated": stats.signals_generated,
            "signalsValidated": stats.signals_validated,
            "signalsRejected": stats.signals_rejected,
            "tradesApproved": stats.trades_approved,
            "tradesRiskRejected": stats.trades_risk_rejected,
            "approvalRate": (
                (stats.signals_validated / stats.signals_generated * 100)
                if stats.signals_generated
                else 0.0
            ),
        },
        "regime": {
            "current": stats.current_regime,
            "confidence": stats.regime_confidence,
            "strategies": list(stats.active_strategies),
        },
        "finops": {
            "calls": stats.llm_calls,
            "tokens": stats.llm_tokens,
            "costUsd": stats.llm_cost_usd,
        },
        "goal": {
            "enabled": stats.goal_enabled,
            "feasible": stats.goal_feasible,
            "targetAmount": stats.goal_target_amount,
            "mtdPnl": stats.goal_mtd_pnl,
            "expectedToDate": stats.goal_expected_to_date,
            "onPace": stats.goal_on_pace,
            "status": stats.goal_status,
        },
        "positions": [
            {
                "symbol": p.get("symbol", "N/A"),
                "side": p.get("side", "N/A"),
                "qty": p.get("qty", 0),
                "entry": p.get("entry", 0.0),
                "pnl": p.get("pnl", 0.0),
            }
            for p in stats.open_positions
        ],
        "quotes": quotes,
        "decision": {
            "signalType": stats.current_signal.get("signal_type", ""),
            "symbol": stats.current_signal.get("symbol", ""),
            "strategy": stats.current_signal.get("strategy", ""),
            "confidence": stats.current_signal.get("confidence", 0.0),
            "reason": stats.last_decision_reason,
        },
        "activity": list(stats.activity_log),
    }


@dataclass
class Span:
    """One agent step within a trade-cycle trace."""

    id: str
    name: str
    label: str
    kind: str  # "llm" | "deterministic" | "support"
    decision: str = ""
    confidence: float | None = None
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "decision": self.decision,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "tokens": self.total_tokens,
            "costUsd": self.cost_usd,
            "latencyMs": self.latency_ms,
            "detail": self.detail,
        }


@dataclass
class CycleTrace:
    """A full trade cycle rendered as an observability trace (a tree of spans)."""

    id: str
    workflow_id: str
    ts: str
    status: str
    duration_ms: int
    regime: str
    regime_confidence: float
    signals_count: int
    approved_count: int
    rejected_count: int
    tokens: int
    cost_usd: float
    spans: list[Span] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflowId": self.workflow_id,
            "ts": self.ts,
            "status": self.status,
            "durationMs": self.duration_ms,
            "regime": self.regime,
            "regimeConfidence": self.regime_confidence,
            "signalsCount": self.signals_count,
            "approvedCount": self.approved_count,
            "rejectedCount": self.rejected_count,
            "tokens": self.tokens,
            "costUsd": self.cost_usd,
            "spans": [s.to_dict() for s in self.spans],
        }


class CycleRecorder:
    """
    Reconstructs trade cycles as traces for the web trace-explorer.

    Usage per cycle::

        recorder.begin()
        final_state = await run_trading_cycle(...)
        trace = recorder.finish(workflow_id, final_state, signals_count, by_agent_after)

    Token/cost per span is derived from the *delta* in the FinOps ``by_agent`` accounting
    across the pipeline call; decision/confidence/reasoning come from the final state.
    """

    def __init__(self, max_history: int = 100) -> None:
        self.max_history = max_history
        self.cycles: list[CycleTrace] = []
        self._t0: float = 0.0
        self._by_agent_before: dict[str, dict[str, float]] = {}

    def begin(self, by_agent_before: dict[str, dict[str, float]]) -> None:
        self._t0 = time.perf_counter()
        # Deep-ish copy of the numeric buckets so a later mutation can't corrupt the baseline.
        self._by_agent_before = {k: dict(v) for k, v in by_agent_before.items()}

    def finish(
        self,
        *,
        workflow_id: str,
        final_state: dict[str, Any],
        signals_count: int,
        by_agent_after: dict[str, dict[str, float]],
        status: str = "success",
    ) -> CycleTrace:
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        regime = str(final_state.get("regime", "unknown"))
        regime_conf = float(final_state.get("regime_confidence", 0) or 0)
        strategies = final_state.get("active_strategies", []) or []
        validated = final_state.get("validated_signals", []) or []
        rejected = final_state.get("rejected_signals", []) or []
        approved = final_state.get("approved_trades", []) or []
        risk_rejected = final_state.get("risk_rejected", []) or []
        risk_warnings = final_state.get("risk_warnings", []) or []

        spans: list[Span] = []
        total_tokens = 0
        total_cost = 0.0

        for name, label, kind in _PIPELINE_SPANS:
            key = _FINOPS_AGENT_KEY.get(name, name)
            before = self._by_agent_before.get(key, {})
            after = by_agent_after.get(key, {})
            in_tok = int(after.get("input_tokens", 0) - before.get("input_tokens", 0))
            out_tok = int(after.get("output_tokens", 0) - before.get("output_tokens", 0))
            cost = float(after.get("cost_usd", 0.0) - before.get("cost_usd", 0.0))
            in_tok = max(0, in_tok)
            out_tok = max(0, out_tok)
            cost = max(0.0, cost)
            total_tokens += in_tok + out_tok
            total_cost += cost

            decision, confidence, reasoning, detail = self._span_context(
                name,
                final_state,
                regime=regime,
                regime_conf=regime_conf,
                strategies=strategies,
                validated=validated,
                rejected=rejected,
                approved=approved,
                risk_rejected=risk_rejected,
                risk_warnings=risk_warnings,
            )

            spans.append(
                Span(
                    id=f"{workflow_id}:{name}",
                    name=name,
                    label=label,
                    kind=kind,
                    decision=decision,
                    confidence=confidence,
                    reasoning=reasoning,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                    detail=detail,
                )
            )

        trace = CycleTrace(
            id=workflow_id,
            workflow_id=workflow_id,
            ts=now_ist().isoformat(),
            status=status,
            duration_ms=duration_ms,
            regime=regime,
            regime_confidence=regime_conf,
            signals_count=signals_count,
            approved_count=len(approved),
            rejected_count=len(rejected) + len(risk_rejected),
            tokens=total_tokens,
            cost_usd=total_cost,
            spans=spans,
        )
        self.cycles.append(trace)
        if len(self.cycles) > self.max_history:
            self.cycles = self.cycles[-self.max_history :]
        return trace

    @staticmethod
    def _span_context(
        name: str,
        final_state: dict[str, Any],
        *,
        regime: str,
        regime_conf: float,
        strategies: list[Any],
        validated: list[Any],
        rejected: list[Any],
        approved: list[Any],
        risk_rejected: list[Any],
        risk_warnings: list[Any],
    ) -> tuple[str, float | None, str, dict[str, Any]]:
        """Extract the decision/confidence/reasoning + audit detail for a given span."""
        if name == "support_agents":
            return (
                "enriched",
                None,
                "News / sentiment / prediction enrichment applied to the state.",
                {"enrichment": "news, sentiment, prediction (non-fatal)"},
            )
        if name == "market_regime":
            return (
                regime,
                regime_conf,
                str(final_state.get("regime_reasoning", "")),
                {"regime": regime, "confidence": regime_conf},
            )
        if name == "strategy_selection":
            return (
                ", ".join(str(s) for s in strategies) or "none",
                None,
                str(final_state.get("strategy_reasoning", "")),
                {"activeStrategies": [str(s) for s in strategies]},
            )
        if name == "signal_validation":
            return (
                f"{len(validated)} validated / {len(rejected)} rejected",
                None,
                "Filtered raw signals; only surviving signals proceed to risk.",
                {"validated": len(validated), "rejected": len(rejected)},
            )
        if name == "risk_compliance":
            return (
                f"{len(approved)} approved / {len(risk_rejected)} blocked",
                None,
                "Deterministic rules engine: sizing, limits, kill-switch (no LLM).",
                {
                    "approved": len(approved),
                    "riskRejected": len(risk_rejected),
                    "warnings": [str(w) for w in risk_warnings],
                },
            )
        return ("", None, "", {})
