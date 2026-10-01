"""
Feeding the existing CLI and web views from the v2 engine (plan M5.6): every refresh rebuilds the
dashboard's :class:`~src.dashboard.cli.TradingStats` from the event store's **projections** (trades,
positions, decisions) and the engine's market service - never from counters the trading loop
keeps on the side, so what the views show is what the store recorded. M9/M10 replace this
adapter with REST projections and the new console.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from src.domain.events import (
    Alert,
    FillReceived,
    KillSwitchChanged,
    OrderRejected,
    OrderSubmitted,
    SessionStateChanged,
    SignalGenerated,
    TradeClosed,
)
from src.domain.types import RiskDecision, RiskOutcome
from src.risk.marks import open_positions
from src.utils.market_time import IST

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.dashboard.cli import TradingStats
    from src.engine.runner import Engine

_ACTIVITY = (
    "SessionStateChanged", "SignalGenerated", "RiskDecision", "OrderSubmitted", "OrderRejected",
    "FillReceived", "TradeClosed", "KillSwitchChanged", "Alert",
)  # fmt: skip


class StatsProjector:
    """Keeps one ``TradingStats`` in step with an engine's store."""

    def __init__(self, stats: TradingStats, engine: Engine) -> None:
        self.stats = stats
        self.engine = engine
        self._seq = 0
        stats.trading_mode = "paper"
        stats.starting_balance = float(engine.config.starting_cash)
        stats.active_strategies = list(engine.decision.config.enabled)

    def refresh(self) -> None:
        engine, stats = self.engine, self.stats
        store = engine.store
        book_id = engine.config.book_id

        trades = store.query("SELECT net_pnl FROM trades WHERE book_id = ?", (book_id,))
        pnls = [float(Decimal(str(t["net_pnl"]))) for t in trades]
        stats.total_trades = len(pnls)
        stats.winning_trades = sum(1 for p in pnls if p > 0)
        stats.losing_trades = sum(1 for p in pnls if p < 0)
        stats.realized_pnl = sum(pnls)
        stats.best_trade = max(pnls, default=0.0)
        stats.worst_trade = min(pnls, default=0.0)

        marks = engine.monitor.marks()
        book = engine.oms.book
        positions: list[dict[str, Any]] = []
        unrealized = Decimal(0)
        for p in open_positions(book, engine.clock.now()):
            avg = p.avg_price or Decimal(0)
            pnl = (marks.get(p.instrument_key, avg) - avg) * p.quantity
            unrealized += pnl
            positions.append({"symbol": p.instrument_key.rsplit(":", 1)[-1],
                              "side": "BUY" if p.quantity > 0 else "SELL",
                              "qty": abs(p.quantity), "entry": float(avg), "pnl": float(pnl)})  # fmt: skip
        stats.open_positions = positions
        stats.unrealized_pnl = float(unrealized)
        stats.current_balance = float(book.cash + book.market_value(marks))

        decisions = store.query("SELECT outcome, kind FROM decisions WHERE book_id = ?", (book_id,))
        opens = [d for d in decisions if d["kind"] in ("open", "increase")]
        stats.trades_approved = sum(1 for d in opens if d["outcome"] in ("APPROVED", "RESIZED"))
        stats.trades_risk_rejected = sum(1 for d in opens if d["outcome"] in ("REJECTED", "HALTED"))
        stats.cycles_run = len(engine.cycles)
        stats.current_regime = engine.regime.value if engine.regime else "unknown"

        quotes: dict[str, dict[str, float]] = {}
        for key, quote in engine.market.quotes().items():
            change = (quote.ltp / quote.prev_close - 1) * 100 if quote.prev_close else 0.0
            quotes[key.rsplit(":", 1)[-1]] = {"last_price": quote.ltp, "change_percent": change}
            stats.data_source = quote.source.value
        stats.market_quotes = quotes

        for event in store.read(since_seq=self._seq, types=list(_ACTIVITY)):
            self._seq = max(self._seq, event.seq or 0)
            self._activity(event.payload, event.ts_utc)

    def _activity(self, payload: object, ts: datetime) -> None:
        stats = self.stats
        line: tuple[str, str] | None = None
        if isinstance(payload, SessionStateChanged):
            line = (f"Session {payload.current}", "INFO")
        elif isinstance(payload, SignalGenerated):
            s = payload.signal
            stats.signals_generated += 1
            if not s.is_shadow:
                stats.current_signal = {"signal_type": s.side.value,
                                        "symbol": s.instrument_key.rsplit(":", 1)[-1],
                                        "strategy": s.strategy, "confidence": s.agreement_score}  # fmt: skip
        elif isinstance(payload, RiskDecision):
            codes = ", ".join(r.code.value for r in payload.reasons) or "all checks passed"
            stats.last_decision_reason = f"{payload.outcome} {payload.qty_approved}: {codes}"
            if payload.outcome in (RiskOutcome.REJECTED, RiskOutcome.HALTED):
                line = (f"Risk {payload.outcome} {payload.instrument_key}: {codes}", "WARNING")
        elif isinstance(payload, OrderSubmitted):
            o = payload.order
            line = (f"Order {o.intent.side} {o.quantity} {o.intent.instrument.symbol} "
                    f"({o.intent.reason})", "TRADE")  # fmt: skip
        elif isinstance(payload, OrderRejected):
            line = (f"Order rejected: {payload.reason}", "ERROR")
        elif isinstance(payload, FillReceived):
            f = payload.fill
            line = (f"Fill {f.side} {f.quantity} @ {f.price}", "SUCCESS")
        elif isinstance(payload, TradeClosed):
            line = (f"Closed {payload.instrument_key.rsplit(':', 1)[-1]} net "
                    f"Rs {payload.net_pnl:,.2f} ({payload.exit_reason})", "TRADE")  # fmt: skip
        elif isinstance(payload, KillSwitchChanged):
            line = (f"Kill switch {payload.name}: {payload.current} ({payload.reason})", "ERROR")
        elif isinstance(payload, Alert) and payload.level in ("WARNING", "CRITICAL"):
            line = (payload.message[:120], "ERROR" if payload.level == "CRITICAL" else "WARNING")
        if line is not None:
            stats.activity_log.append(
                {"time": ts.astimezone(IST).strftime("%H:%M:%S"), "level": line[1],
                 "message": line[0]}
            )  # fmt: skip
            stats.activity_log = stats.activity_log[-12:]
