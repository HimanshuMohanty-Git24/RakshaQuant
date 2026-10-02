"""
The shadow ledger (plan M8.3; audit §Y.1): the counterfactual of **every** signal - approved,
vetoed (in any book), risk-rejected, or from a shadow strategy - simulated on the live tape with
the same TradePolicy exit rules, net of modelled NSE costs, settled at exit.

* **Entry** - the first quote received after the signal's decision, at a standard notional
  (``notional``; default ₹1,00,000 = the 10% position cap on ₹10L), whole shares.
* **Exits** (the ExitManager's rules) - stop = entry − ``k_stop``·ATR, target = entry +
  ``k_target``·ATR, checked on every quote (a quote through the stop exits at that quote: gaps
  are taken, not assumed away); a trailing stop on daily closes; a time exit at the first quote of
  the session after ``max_hold_days``. Partial exits are not modelled.
* **Costs** - the same :class:`NSECostSchedule` as the simulated broker (CNC, DP on the sell).
* **Alpha** - once the NIFTY close for the exit session is known (the next pre-open), the excess
  return over NIFTY from the close before entry to the close of the exit session.

Long-only: SELL signals have no counterfactual. Which book did what with each signal is in the
``SignalDisposition`` events; the daily report joins the two (e.g. veto precision).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict

from src.brokers.simulated.costs import NSECostSchedule
from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.clock import Clock
from src.domain.events import ShadowAlphaSettled, ShadowTradeClosed, ShadowTradeOpened
from src.domain.sink import EventSink
from src.domain.types import Product, Quote, Side, Signal
from src.oms.exit_manager import ExitPolicy
from src.store.kv import MemoryStateStore, StateStore
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

DEFAULT_NOTIONAL = Decimal(100_000)


class _Pending(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    decision_id: str
    instrument_key: str
    strategy: str
    is_shadow: bool
    decided_at: datetime
    atr: Decimal


class _Open(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    decision_id: str
    instrument_key: str
    strategy: str
    is_shadow: bool
    entry_ts: datetime
    entry_price: Decimal
    quantity: int
    stop: Decimal
    target: Decimal
    atr: Decimal
    highest_close: Decimal
    entered_on: date
    time_exit: bool = False


class _Settling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    decision_id: str
    instrument_key: str
    entry_date: date
    exit_date: date
    net_return_pct: float


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seen: set[str] = set()
    pending: list[_Pending] = []
    open: list[_Open] = []
    settling: list[_Settling] = []


class ShadowLedger:
    def __init__(
        self,
        *,
        policy: ExitPolicy,
        costs: NSECostSchedule,
        calendar: NSECalendar,
        clock: Clock,
        sink: EventSink,
        notional: Decimal = DEFAULT_NOTIONAL,
        state_store: StateStore | None = None,
    ) -> None:
        if notional <= 0:
            raise ValueError("notional must be positive")
        self.policy = policy
        self._costs = costs
        self._calendar = calendar
        self._clock = clock
        self._sink = sink
        self._notional = notional
        self._store = state_store or MemoryStateStore()
        saved = self._store.load()
        self._state = _State.model_validate_json(saved) if saved else _State()

    @property
    def open_count(self) -> int:
        return len(self._state.open)

    # -- signals -------------------------------------------------------------------------------

    def track(self, signals: Iterable[Signal], atr: Mapping[str, Decimal | None]) -> int:
        """Queue a counterfactual for each new BUY signal with an ATR; returns how many."""
        added = 0
        for signal in signals:
            value = atr.get(signal.instrument_key)
            if (signal.signal_id in self._state.seen or signal.side is not Side.BUY
                    or value is None or value <= 0):  # fmt: skip
                continue
            self._state.seen.add(signal.signal_id)
            self._state.pending.append(_Pending(
                signal_id=signal.signal_id, decision_id=signal.decision_id,
                instrument_key=signal.instrument_key, strategy=signal.strategy,
                is_shadow=signal.is_shadow, decided_at=signal.generated_at, atr=value,
            ))  # fmt: skip
            added += 1
        self._save()
        return added

    # -- the tape ----------------------------------------------------------------------------------

    async def on_quote(self, quote: Quote) -> None:
        key, price, ts = quote.instrument_key, Decimal(str(quote.ltp)), quote.receipt_ts
        changed = False
        for pending in [p for p in self._state.pending if p.instrument_key == key]:
            if ts > pending.decided_at:
                self._state.pending.remove(pending)
                self._open(pending, price, ts)
                changed = True
        for trade in [t for t in self._state.open if t.instrument_key == key]:
            if ts <= trade.entry_ts:
                continue
            reason = ("time" if trade.time_exit else "stop" if price <= trade.stop
                      else "target" if price >= trade.target else None)  # fmt: skip
            if reason is not None:
                self._close(trade, price, ts, reason)
                changed = True
        if changed:
            self._save()

    def on_session_start(self, day: date, *, closes: Mapping[str, Decimal],
                         atr: Mapping[str, Decimal]) -> None:  # fmt: skip
        """Daily: time exits are due at the first quote; the stop trails the highest close."""
        for trade in self._state.open:
            if self._sessions_held(trade.entered_on, day) >= self.policy.max_hold_days:
                trade.time_exit = True
                continue
            close = closes.get(trade.instrument_key)
            if close is not None:
                trade.highest_close = max(trade.highest_close, close)
            if self.policy.k_trail_atr is not None:
                current = atr.get(trade.instrument_key, trade.atr)
                trail = trade.highest_close - Decimal(str(self.policy.k_trail_atr)) * current
                trade.stop = max(trade.stop, trail)
        self._save()

    def settle_alpha(self, nifty_closes: Mapping[date, float]) -> int:
        """Settle every closed counterfactual whose NIFTY closes are now known."""
        settled = 0
        for item in list(self._state.settling):
            try:
                start_day = self._calendar.previous_trading_day(item.entry_date)
            except CalendarCoverageError:
                continue
            start, end = nifty_closes.get(start_day), nifty_closes.get(item.exit_date)
            if start is None or end is None:
                continue
            nifty = (end / start - 1) * 100
            self._sink.emit(ShadowAlphaSettled(
                signal_id=item.signal_id, decision_id=item.decision_id,
                instrument_key=item.instrument_key, entry_date=item.entry_date,
                exit_date=item.exit_date, net_return_pct=item.net_return_pct,
                nifty_return_pct=round(nifty, 4), alpha_pct=round(item.net_return_pct - nifty, 4),
            ), source="shadow_ledger")  # fmt: skip
            self._state.settling.remove(item)
            settled += 1
        if settled:
            self._save()
        return settled

    # -- internals -----------------------------------------------------------------------------------

    def _open(self, p: _Pending, price: Decimal, ts: datetime) -> None:
        quantity = int((self._notional / price).to_integral_value(rounding=ROUND_FLOOR))
        if quantity < 1:
            return
        stop = price - Decimal(str(self.policy.k_stop_atr)) * p.atr
        target = price + Decimal(str(self.policy.k_target_atr)) * p.atr
        trade = _Open(signal_id=p.signal_id, decision_id=p.decision_id,
                      instrument_key=p.instrument_key, strategy=p.strategy, is_shadow=p.is_shadow,
                      entry_ts=ts, entry_price=price, quantity=quantity, stop=stop, target=target,
                      atr=p.atr, highest_close=price, entered_on=ts.astimezone(IST).date())  # fmt: skip
        self._state.open.append(trade)
        self._sink.emit(ShadowTradeOpened(
            signal_id=p.signal_id, decision_id=p.decision_id, instrument_key=p.instrument_key,
            strategy=p.strategy, is_shadow_strategy=p.is_shadow, entry_ts=ts, entry_price=price,
            quantity=quantity, stop_price=max(stop, Decimal("0.01")), target_price=target,
            atr=p.atr,
        ), source="shadow_ledger")  # fmt: skip

    def _close(self, t: _Open, price: Decimal, ts: datetime, reason: str) -> None:
        self._state.open.remove(t)
        buy = self._costs.charges(product=Product.CNC, side=Side.BUY,
                                  notional=t.entry_price * t.quantity)  # fmt: skip
        sell = self._costs.charges(product=Product.CNC, side=Side.SELL,
                                   notional=price * t.quantity, dp_applies=True)  # fmt: skip
        charges = buy.total + sell.total
        gross = (price - t.entry_price) * t.quantity
        net = gross - charges
        ret = float(net / (t.entry_price * t.quantity) * 100)
        exit_day = ts.astimezone(IST).date()
        self._sink.emit(ShadowTradeClosed(
            signal_id=t.signal_id, decision_id=t.decision_id, instrument_key=t.instrument_key,
            strategy=t.strategy, is_shadow_strategy=t.is_shadow, entry_ts=t.entry_ts,
            entry_price=t.entry_price, exit_ts=ts, exit_price=price, exit_reason=reason,
            quantity=t.quantity, gross_pnl=gross, charges=charges, net_pnl=net,
            net_return_pct=round(ret, 4), hold_sessions=self._sessions_held(t.entered_on, exit_day),
        ), source="shadow_ledger")  # fmt: skip
        self._state.settling.append(_Settling(
            signal_id=t.signal_id, decision_id=t.decision_id, instrument_key=t.instrument_key,
            entry_date=t.entered_on, exit_date=exit_day, net_return_pct=round(ret, 4),
        ))  # fmt: skip

    def _sessions_held(self, start: date, day: date) -> int:
        try:
            return max(0, len(self._calendar.trading_days(start, day)) - 1)
        except CalendarCoverageError:
            return max(0, (day - start).days)

    def _save(self) -> None:
        self._store.save(self._state.model_dump_json(), self._clock.now())
