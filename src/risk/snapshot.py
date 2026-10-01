"""
What a risk decision is made against (plan M4.1; audit §L.3).

* :class:`RiskSnapshot` - the book's and the market's state at one instant (built by the caller
  from the PositionBook, DailyRiskState, kill switches, quotes and history).
* :class:`Reservations` - capacity already granted to earlier proposals in the same decision
  batch, so five signals in one cycle cannot each see the same free capacity (audit F-13).
* :class:`RiskContext` - one intent's view: snapshot + reservations + limits + derived prices.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.config.limits import RiskLimits
from src.domain.types import (
    Instrument,
    KillState,
    MarketDataSource,
    OrderIntent,
    Quote,
    RiskSnapshotSummary,
)

UNKNOWN_SECTOR = "Unknown"  # one bucket for unmapped symbols (audit §L.2)


@dataclass(frozen=True)
class PositionInfo:
    instrument_key: str
    quantity: int  # signed
    avg_price: Decimal
    mark: Decimal
    sector: str | None = None
    strategy: str | None = None
    stop_price: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return abs(self.quantity) * self.mark

    @property
    def open_risk(self) -> Decimal:
        """Loss to the stop (or the full notional when there is no stop: it is unbounded)."""
        if self.stop_price is None:
            return self.notional
        return max(Decimal(0), (self.mark - self.stop_price) * self.quantity)


@dataclass(frozen=True)
class StrategyStats:
    day_pnl: Decimal = Decimal(0)
    consecutive_losses: int = 0
    orders_last_min: int = 0
    trades: int = 0  # real, closed, net trades (for Kelly)
    win_rate: float | None = None
    payoff: float | None = None  # average win / average loss


@dataclass(frozen=True)
class MarketFacts:
    quote: Quote | None = None
    atr: Decimal | None = None
    adv_shares: float | None = None
    data_source: MarketDataSource | None = None


@dataclass(frozen=True)
class RiskSnapshot:
    now: datetime
    environment: str
    equity: Decimal
    cash: Decimal
    sod_equity: Decimal
    peak_equity: Decimal
    positions: Mapping[str, PositionInfo] = field(default_factory=dict)
    market: Mapping[str, MarketFacts] = field(default_factory=dict)
    strategies: Mapping[str, StrategyStats] = field(default_factory=dict)
    kill: KillState = field(default_factory=KillState)
    open_orders: int = 0
    orders_last_min: int = 0
    rejects_in_window: int = 0
    entries_today: int = 0
    symbols_entered_today: frozenset[str] = frozenset()
    symbols_exited_today: frozenset[str] = frozenset()
    unknown_order_keys: frozenset[str] = frozenset()
    is_trading_day: bool = True
    is_weekend: bool = False
    session_open: bool = True
    entry_window: bool = True
    llm_degraded: bool = False
    journal_durable: bool = True
    recon_drift: bool = False

    @property
    def day_pnl_mtm(self) -> Decimal:
        return self.equity - self.sod_equity

    @property
    def gross_exposure(self) -> Decimal:
        return sum((p.notional for p in self.positions.values()), Decimal(0))

    @property
    def net_exposure(self) -> Decimal:
        return sum((p.quantity * p.mark for p in self.positions.values()), Decimal(0))

    @property
    def heat(self) -> Decimal:
        return sum((p.open_risk for p in self.positions.values()), Decimal(0))

    def sector_exposure(self, sector: str) -> Decimal:
        return sum(
            (p.notional for p in self.positions.values() if (p.sector or UNKNOWN_SECTOR) == sector),
            Decimal(0),
        )

    def deployed(self, strategy: str) -> Decimal:
        return sum(
            (p.notional for p in self.positions.values() if p.strategy == strategy), Decimal(0)
        )

    def summary(
        self, quote_age_s: float | None, source: MarketDataSource | None
    ) -> RiskSnapshotSummary:
        return RiskSnapshotSummary(
            equity=self.equity,
            sod_equity=self.sod_equity,
            day_pnl_mtm=self.day_pnl_mtm,
            peak_equity=self.peak_equity,
            gross_exposure=self.gross_exposure,
            net_exposure=self.net_exposure,
            heat=self.heat,
            open_positions=sum(1 for p in self.positions.values() if p.quantity),
            open_orders=self.open_orders,
            quote_age_s=None if quote_age_s is None else max(0.0, quote_age_s),
            data_source=source,
        )


@dataclass
class Reservations:
    """Capacity granted to earlier approvals in the same decision batch."""

    symbols: set[str] = field(default_factory=set)
    gross: Decimal = Decimal(0)
    heat: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    entries: int = 0
    orders: int = 0
    by_sector: dict[str, Decimal] = field(default_factory=dict)
    by_strategy: dict[str, Decimal] = field(default_factory=dict)

    def add(self, ctx: RiskContext, quantity: int) -> None:
        if not ctx.opening or ctx.price is None:
            self.orders += 1
            return
        notional = ctx.price * quantity
        self.symbols.add(ctx.intent.instrument.key)
        self.gross += notional
        self.cash += notional
        self.entries += 1
        self.orders += 1
        if ctx.stop_distance is not None:
            self.heat += ctx.stop_distance * quantity
        sector = ctx.sector
        self.by_sector[sector] = self.by_sector.get(sector, Decimal(0)) + notional
        strategy = ctx.intent.strategy
        self.by_strategy[strategy] = self.by_strategy.get(strategy, Decimal(0)) + notional


@dataclass(frozen=True)
class RiskContext:
    intent: OrderIntent
    snapshot: RiskSnapshot
    limits: RiskLimits
    reserved: Reservations
    requested_qty: int | None

    @property
    def instrument(self) -> Instrument:
        return self.intent.instrument

    @property
    def opening(self) -> bool:
        return not self.intent.kind.reduces_risk

    @property
    def facts(self) -> MarketFacts:
        return self.snapshot.market.get(self.instrument.key, MarketFacts())

    @property
    def price(self) -> Decimal | None:
        """The fresh reference price (the latest quote), never the decision snapshot."""
        quote = self.facts.quote
        return None if quote is None else Decimal(str(quote.ltp))

    @property
    def stop_distance(self) -> Decimal | None:
        stop, price = self.intent.stop_price, self.price
        if stop is None or price is None:
            return None
        return price - stop if self.intent.side.sign > 0 else stop - price

    @property
    def sector(self) -> str:
        return self.instrument.sector or UNKNOWN_SECTOR

    @property
    def position(self) -> PositionInfo | None:
        return self.snapshot.positions.get(self.instrument.key)

    @property
    def equity(self) -> Decimal:
        return self.snapshot.equity

    def pct(self, fraction: float) -> Decimal:
        """``fraction`` of equity, in rupees."""
        return self.snapshot.equity * Decimal(str(fraction))
