"""
The persisted per-IST-day risk state of one book (plan M4.3; audit §L.4).

:class:`DailyRiskState` holds what the limits are measured against: start-of-day equity, the
running peak, net realised P&L, entries, per-strategy P&L and loss streaks, order timestamps,
reject timestamps and the breaches already raised today. It is saved on every change and
**seeded from the store at startup, so a restart never clears a breach**.

:meth:`DailyRiskTracker.risk_tick` runs in the monitor task (every 60 s in month 1), independently
of signal cycles: it marks equity to market, updates the peak, and returns the *new* breaches -
daily loss vs start-of-day equity, drawdown vs the peak, strategy loss/streak limits and reject
storms - for the kill-switch registry to act on (HALT_NEW or FLATTEN, per the limits).

A new IST day rolls the state: the peak and the strategies' loss streaks carry over (a streak
that straddles midnight is still a streak); P&L, entries, symbols, orders and breaches reset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.config.limits import KillAction, RiskLimits
from src.domain.base import EventPayload
from src.domain.clock import Clock, now_ist
from src.domain.events import (
    DailyRiskStateRolled,
    LimitBreached,
    OrderRejected,
    OrderSubmitted,
    TradeClosed,
)
from src.domain.sink import EventSink
from src.domain.types import KillScope, ReasonCode
from src.risk.snapshot import StrategyStats
from src.store.kv import MemoryStateStore, StateStore

logger = logging.getLogger(__name__)

R = ReasonCode
ORDER_RATE_WINDOW = timedelta(seconds=60)


class StrategyDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized_pnl: Decimal = Decimal(0)
    trades: int = 0
    consecutive_losses: int = 0
    entries: int = 0
    order_ts: list[datetime] = []
    last_exit: str | None = None  # the exit a streak was last counted for (pieces share it)


class DailyRiskState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: str
    ist_date: date
    sod_equity: Decimal
    peak_equity: Decimal
    last_equity: Decimal
    low_equity: Decimal
    realized_pnl: Decimal = Decimal(0)
    entries: int = 0
    symbols_entered: set[str] = set()
    symbols_exited: set[str] = set()
    strategies: dict[str, StrategyDay] = {}
    order_ts: list[datetime] = []
    reject_ts: list[datetime] = []
    breaches: set[str] = set()  # "<code>:<name>" already raised today
    last_tick: datetime | None = None

    @property
    def day_pnl_mtm(self) -> Decimal:
        return self.last_equity - self.sod_equity

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return max(Decimal(0), (self.peak_equity - self.last_equity) / self.peak_equity)


@dataclass(frozen=True)
class Breach:
    """A limit crossed: the kill switch ``scope``/``name`` must move to ``action``."""

    scope: KillScope
    name: str  # "global" or the strategy
    action: KillAction
    code: ReasonCode
    observed: float
    limit: float
    message: str


class DailyRiskTracker:
    def __init__(
        self,
        *,
        book_id: str,
        limits: RiskLimits,
        clock: Clock,
        sink: EventSink,
        state_store: StateStore | None = None,
    ) -> None:
        self.book_id = book_id
        self.limits = limits
        self._clock = clock
        self._sink = sink
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._state = DailyRiskState.model_validate_json(saved) if saved else None
        if self._state is not None and self._state.book_id != book_id:
            raise ValueError(f"stored risk state is for book {self._state.book_id}")

    @property
    def state(self) -> DailyRiskState | None:
        return self._state

    def today(self) -> date:
        return now_ist(self._clock).date()

    # -- the day ---------------------------------------------------------------------------------

    def start_day(self, sod_equity: Decimal) -> DailyRiskState:
        """Today's state. A stored state for today is kept unchanged (a restart never resets the
        start-of-day equity or clears a breach); a new IST day rolls from the previous one."""
        today = self.today()
        previous = self._state
        if previous is not None and previous.ist_date == today:
            return previous
        if previous is not None:
            if previous.ist_date > today:
                raise ValueError(f"stored risk state is dated {previous.ist_date}, after {today}")
            self._sink.emit(
                DailyRiskStateRolled(
                    book_id=self.book_id,
                    ist_date=previous.ist_date,
                    state=previous.model_dump(mode="json"),
                ),
                source="risk",
            )
        peak = sod_equity if previous is None else max(previous.peak_equity, sod_equity)
        streaks = {} if previous is None else previous.strategies
        self._state = DailyRiskState(
            book_id=self.book_id,
            ist_date=today,
            sod_equity=sod_equity,
            peak_equity=peak,
            last_equity=sod_equity,
            low_equity=sod_equity,
            strategies={
                name: StrategyDay(
                    consecutive_losses=day.consecutive_losses, last_exit=day.last_exit
                )
                for name, day in streaks.items()
            },
        )
        logger.info("risk day %s for book %s: start-of-day equity %s, peak %s",
                    today, self.book_id, sod_equity, peak)  # fmt: skip
        self._save()
        return self._state

    def _current(self, equity_hint: Decimal | None = None) -> DailyRiskState:
        """Today's state, rolling from the last mark if the monitor never started the day."""
        state = self._state
        if state is not None and state.ist_date == self.today():
            return state
        if equity_hint is not None:
            return self.start_day(equity_hint)
        if state is not None:
            return self.start_day(state.last_equity)
        raise RuntimeError("start_day() must run before the first risk event")

    # -- what happened -------------------------------------------------------------------------------

    def on_event(self, payload: EventPayload) -> None:
        """Feed from the OMS's event stream (see :meth:`OMS.add_event_listener`)."""
        if isinstance(payload, OrderSubmitted):
            self.record_order(payload)
        elif isinstance(payload, OrderRejected):
            self.record_reject()
        elif isinstance(payload, TradeClosed):
            self.record_trade(payload)

    def record_order(self, submitted: OrderSubmitted) -> None:
        state = self._current()
        now = self._clock.now()
        intent = submitted.order.intent
        day = state.strategies.setdefault(intent.strategy, StrategyDay())
        state.order_ts = [*_recent(state.order_ts, now, ORDER_RATE_WINDOW), now]
        day.order_ts = [*_recent(day.order_ts, now, ORDER_RATE_WINDOW), now]
        if not intent.kind.reduces_risk:
            state.entries += 1
            day.entries += 1
            state.symbols_entered.add(intent.instrument.key)
        self._save()

    def record_reject(self) -> None:
        state = self._current()
        now = self._clock.now()
        window = timedelta(seconds=self.limits.reject_storm_window_s)
        state.reject_ts = [*_recent(state.reject_ts, now, window), now]
        self._save()

    def record_trade(self, trade: TradeClosed) -> None:
        state = self._current()
        day = state.strategies.setdefault(trade.strategy, StrategyDay())
        state.realized_pnl += trade.net_pnl
        day.realized_pnl += trade.net_pnl
        state.symbols_exited.add(trade.instrument_key)
        # One exit can close several lots (several pieces): it is one win or loss for the streak.
        exit_ref = f"{trade.exit_decision_id or trade.trade_id}|{trade.instrument_key}"
        if exit_ref != day.last_exit:
            day.last_exit = exit_ref
            day.trades += 1
            if trade.net_pnl < 0:
                day.consecutive_losses += 1
            elif trade.net_pnl > 0:
                day.consecutive_losses = 0
        self._save()

    # -- the monitor tick ------------------------------------------------------------------------------

    def risk_tick(
        self, equity: Decimal, *, unrealized_by_strategy: dict[str, Decimal] | None = None
    ) -> list[Breach]:
        """Mark to market and return the limits newly crossed (each raised once per IST day)."""
        state = self._current(equity)
        now = self._clock.now()
        state.last_equity = equity
        state.low_equity = min(state.low_equity, equity)
        state.peak_equity = max(state.peak_equity, equity)
        state.last_tick = now
        limits = self.limits

        found: list[Breach] = []
        loss = state.sod_equity - equity
        loss_limit = state.sod_equity * Decimal(str(limits.daily_loss_limit_pct))
        if state.sod_equity > 0 and loss >= loss_limit:
            found.append(Breach(KillScope.GLOBAL, "global", limits.daily_loss_action,
                                R.PF_DAILY_LOSS_MTM, _f(loss / state.sod_equity),
                                limits.daily_loss_limit_pct,
                                f"day loss Rs {loss:,.2f} vs limit Rs {loss_limit:,.2f}"))  # fmt: skip
        if state.drawdown >= Decimal(str(limits.max_drawdown_pct)):
            found.append(Breach(KillScope.GLOBAL, "global", limits.drawdown_action, R.PF_DRAWDOWN,
                                _f(state.drawdown), limits.max_drawdown_pct,
                                f"equity Rs {equity:,.2f} is {state.drawdown:.2%} below the peak "
                                f"Rs {state.peak_equity:,.2f}"))  # fmt: skip
        rejects = len(
            _recent(state.reject_ts, now, timedelta(seconds=limits.reject_storm_window_s))
        )
        if rejects >= limits.reject_storm_count:
            found.append(Breach(KillScope.GLOBAL, "global", "HALT_NEW", R.SYS_REJECT_STORM,
                                rejects, limits.reject_storm_count,
                                f"{rejects} rejects in {limits.reject_storm_window_s:.0f} s"))  # fmt: skip

        unrealized = unrealized_by_strategy or {}
        strategy_limit = state.sod_equity * Decimal(str(limits.strategy_daily_loss_pct))
        for name in sorted(state.strategies.keys() | unrealized.keys()):
            day = state.strategies.get(name, StrategyDay())
            pnl = day.realized_pnl + unrealized.get(name, Decimal(0))
            if state.sod_equity > 0 and -pnl >= strategy_limit:
                found.append(Breach(KillScope.STRATEGY, name, "HALT_NEW", R.STR_DAILY_LOSS,
                                    _f(-pnl / state.sod_equity), limits.strategy_daily_loss_pct,
                                    f"{name} day P&L Rs {pnl:,.2f}"))  # fmt: skip
            if day.consecutive_losses >= limits.strategy_max_consec_losses:
                found.append(Breach(KillScope.STRATEGY, name, "HALT_NEW", R.STR_CONSEC_LOSSES,
                                    day.consecutive_losses, limits.strategy_max_consec_losses,
                                    f"{name} lost {day.consecutive_losses} trades in a row"))  # fmt: skip

        new = [b for b in found if f"{b.code}:{b.name}" not in state.breaches]
        for breach in new:
            state.breaches.add(f"{breach.code}:{breach.name}")
            logger.warning("limit breached: %s %s (%s)", breach.code, breach.name, breach.message)
            self._sink.emit(
                LimitBreached(book_id=self.book_id, code=breach.code, observed=breach.observed,
                              limit=breach.limit, message=breach.message),
                source="risk",
            )  # fmt: skip
        self._save()
        return new

    # -- what the RiskEngine reads ---------------------------------------------------------------------

    def orders_last_min(self) -> int:
        state = self._state
        return (
            0
            if state is None
            else len(_recent(state.order_ts, self._clock.now(), ORDER_RATE_WINDOW))
        )

    def rejects_in_window(self) -> int:
        state = self._state
        if state is None:
            return 0
        window = timedelta(seconds=self.limits.reject_storm_window_s)
        return len(_recent(state.reject_ts, self._clock.now(), window))

    def strategy_stats(
        self, unrealized_by_strategy: dict[str, Decimal] | None = None
    ) -> dict[str, StrategyStats]:
        state = self._state
        if state is None or state.ist_date != self.today():
            return {}
        now = self._clock.now()
        unrealized = unrealized_by_strategy or {}
        return {
            name: StrategyStats(
                day_pnl=day.realized_pnl + unrealized.get(name, Decimal(0)),
                consecutive_losses=day.consecutive_losses,
                orders_last_min=len(_recent(day.order_ts, now, ORDER_RATE_WINDOW)),
            )
            for name, day in state.strategies.items()
        }

    def _save(self) -> None:
        if self._state is not None:
            self._store.save(self._state.model_dump_json(), self._clock.now())


def _recent(stamps: list[datetime], now: datetime, window: timedelta) -> list[datetime]:
    return [ts for ts in stamps if now - ts < window]


def _f(value: Decimal) -> float:
    return round(float(value), 6)
