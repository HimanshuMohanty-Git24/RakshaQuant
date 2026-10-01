"""
TradePolicy (plan M5.2): the one place a signal becomes an order proposal - shared by the live
decision engine, the shadow ledger (M8) and the backtest (M11).

Month 1 is **long-only CNC** swing: a BUY signal becomes an unsized ``OPEN`` intent (the
RiskEngine sizes it); SELL signals and shadow strategies never become orders. The intent's stop
and target are ``price ∓ k·ATR`` at the decision price, for the pre-trade checks; the live levels
are re-anchored to the **fill** by the ExitManager, using :meth:`TradePolicyConfig.exit_policy`
(the same multipliers, ``max_hold_days``, trailing and partial rules).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.domain.events import OrderIntentProposed
from src.domain.ids import intent_id
from src.domain.sink import EventSink
from src.domain.types import (
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    OrderIntent,
    OrderType,
    Product,
    Side,
    Signal,
)
from src.oms.exit_manager import ExitPolicy


@dataclass(frozen=True)
class TradePolicyConfig:
    k_stop_atr: float = 2.0
    k_target_atr: float = 3.0
    k_trail_atr: float | None = 2.0
    max_hold_days: int = 10
    partial_at_r: float | None = None
    partial_fraction: float = 0.5
    product: Product = Product.CNC

    def __post_init__(self) -> None:
        self.exit_policy()  # the same validation as the exit manager's
        if self.product is not Product.CNC:
            raise ValueError("month 1 trades CNC only")

    def exit_policy(self) -> ExitPolicy:
        return ExitPolicy(
            k_stop_atr=self.k_stop_atr,
            k_target_atr=self.k_target_atr,
            k_trail_atr=self.k_trail_atr,
            max_hold_days=self.max_hold_days,
            partial_at_r=self.partial_at_r,
            partial_fraction=self.partial_fraction,
        )


@dataclass(frozen=True)
class Proposal:
    """An entry the policy proposes, with the ATR the exit manager anchors to the fill."""

    intent: OrderIntent
    signal: Signal
    atr: Decimal


@dataclass(frozen=True)
class Skipped:
    signal: Signal
    reason: str  # "shadow", "long_only", "no_atr"


class TradePolicy:
    def __init__(self, config: TradePolicyConfig | None = None) -> None:
        self.config = config or TradePolicyConfig()

    def propose(
        self,
        signal: Signal,
        instrument: Instrument,
        *,
        book_id: str,
        price: Decimal,
        atr: Decimal | None,
        decision_ts: datetime,
        sink: EventSink | None = None,
    ) -> Proposal | Skipped:
        """The entry for ``signal`` at decision ``price`` (or why there is none)."""
        if signal.instrument_key != instrument.key:
            raise ValueError(f"signal for {signal.instrument_key}, instrument {instrument.key}")
        if signal.is_shadow:
            return Skipped(signal, "shadow")
        if signal.side is not Side.BUY:
            return Skipped(signal, "long_only")
        if atr is None or atr <= 0 or price <= 0:
            return Skipped(signal, "no_atr")
        stop = price - Decimal(str(signal.stop_atr_mult)) * atr
        if stop <= 0:
            return Skipped(signal, "no_atr")
        intent = OrderIntent(
            intent_id=intent_id(
                book_id=book_id,
                strategy=signal.strategy,
                instrument_key=instrument.key,
                signal_bar_date=signal.bar_date,
                leg="entry",
            ),  # fmt: skip
            decision_id=signal.decision_id,
            book_id=book_id,
            strategy=signal.strategy,
            signal_id=signal.signal_id,
            instrument=instrument,
            side=Side.BUY,
            kind=IntentKind.OPEN,
            quantity=None,  # sized by the RiskEngine
            order_type=OrderType.MARKET,
            product=self.config.product,
            stop_price=stop,
            target_price=price + Decimal(str(signal.target_atr_mult)) * atr,
            reduce_only=False,
            decision_price=price,
            decision_ts=decision_ts,
            reason=IntentReason.ENTRY,
            source=IntentSource.SIGNAL_ENGINE,
        )
        if sink is not None:
            sink.emit(OrderIntentProposed(intent=intent), source="policy")
        return Proposal(intent, signal, atr)
