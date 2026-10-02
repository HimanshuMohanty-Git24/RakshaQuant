"""
The deterministic decision engine (plan M5.4; audit §F.2): plain async, no LangGraph, run by the
engine once per session in the ENTRY_WINDOW.

One cycle:

1. **Signals on settled bars.** For every instrument in the universe: features on its settled,
   dividend-adjusted daily bars (a series that is *lagging* - Yahoo's newest close still NaN - is
   skipped, never traded on stale bars), then every enabled strategy (may trade) and every shadow
   strategy (recorded only). Each signal is a ``SignalGenerated`` event.
2. **Re-quote** the candidate instruments immediately before submitting.
3. **Policy.** :class:`~src.strategies.policy.TradePolicy` turns each tradeable BUY signal into an
   unsized entry: decision price = the signal bar's settled close; stop/target around the fresh
   (arrival) quote. Optional regime gate per strategy.
4. **Per book** (plan M8.2; books A/B/C share steps 1-3 and the ``decision_id``): the book's
   proposal (its own ``intent_id``), the book's **advisor** (may only veto; none = the
   deterministic decision stands), then ``OMS.submit`` on the book's own OMS in descending
   ``agreement_score`` (the book's RiskEngine sizes, reserves capacity, and may reject), after
   telling the book's ExitManager the entry's ATR.
5. Every (signal, book) outcome is a ``SignalDisposition`` event, for the shadow ledger.

The fill price completes the price lineage: decision (bar close, on the intent) → arrival (fresh
quote, the RiskDecision's ``ref_price``) → fill (``FillReceived``).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from src.domain.clock import Clock
from src.domain.events import Alert, Disposition, SignalDisposition, SignalGenerated
from src.domain.ids import new_id
from src.domain.sink import EventSink
from src.domain.types import Instrument, Quote, Regime, Signal, Verdict
from src.features.regime import regime_allows
from src.features.technical import Features, compute_features
from src.marketdata.history import DailySeries
from src.oms.exit_manager import ExitManager
from src.oms.oms import OMS, SubmitResult
from src.strategies import Strategy, generate_signals
from src.strategies.policy import Proposal, Skipped, TradePolicy

logger = logging.getLogger(__name__)


class MarketView(Protocol):
    """What the decision engine reads from market data."""

    def daily(self, instrument_key: str) -> DailySeries | None:
        """Settled daily bars (None when there is no usable history)."""
        ...

    def is_lagging(self, instrument_key: str) -> bool:
        """The series ends before the previous session (its newest close is missing)."""
        ...

    async def requote(self, instrument_keys: Collection[str]) -> Mapping[str, Quote]:
        """Fresh quotes, fetched now (the source keeps them for the RiskGate too)."""
        ...


class Advisor(Protocol):
    """A per-book reviewer (M8). It may only veto; it never changes price, size, stop or target."""

    async def review(self, proposal: Proposal, features: Features) -> Verdict: ...


@dataclass
class BookTarget:
    """One book's order path: its OMS (behind its own RiskGate), exit manager and advisor."""

    book_id: str
    oms: OMS
    exits: ExitManager
    advisor: Advisor | None = None


@dataclass(frozen=True)
class DecisionConfig:
    book_id: str = "A"
    enabled: tuple[str, ...] = ("momentum", "mean_reversion")
    shadow: tuple[str, ...] = ("breakout", "trend_following")
    regime_gates: Mapping[str, frozenset[Regime]] = field(default_factory=dict)


@dataclass
class CycleResult:
    cycle_id: str
    signals: list[Signal] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # instrument key / signal id -> why
    proposals: list[Proposal] = field(default_factory=list)
    vetoed: list[Proposal] = field(default_factory=list)
    submitted: list[tuple[Proposal, SubmitResult]] = field(default_factory=list)

    @property
    def accepted(self) -> list[Proposal]:
        return [p for p, r in self.submitted if r.accepted]


class DecisionEngine:
    def __init__(
        self,
        *,
        config: DecisionConfig,
        market: MarketView,
        policy: TradePolicy,
        clock: Clock,
        sink: EventSink,
        books: Sequence[BookTarget] | None = None,
        oms: OMS | None = None,
        exits: ExitManager | None = None,
        advisor: Advisor | None = None,
        strategies: Mapping[str, Strategy] | None = None,
    ) -> None:
        if books is None:  # one book: the single-book form
            if oms is None or exits is None:
                raise ValueError("give either books or an oms and exits")
            if oms.book_id != config.book_id:
                raise ValueError("the decision engine and its OMS must share the book id")
            books = [BookTarget(config.book_id, oms, exits, advisor)]
        if not books or len({b.book_id for b in books}) != len(books):
            raise ValueError("books must be non-empty with distinct ids")
        for book in books:
            if book.oms.book_id != book.book_id:
                raise ValueError(f"book {book.book_id}'s OMS is for book {book.oms.book_id}")
        self.config = config
        self.books = {b.book_id: b for b in books}
        self._market = market
        self._policy = policy
        self._clock = clock
        self._sink = sink
        self._strategies = strategies

    @property
    def _exits(self) -> ExitManager:  # the first book's (single-book callers and tests)
        return next(iter(self.books.values())).exits

    async def run_cycle(
        self, universe: Sequence[Instrument], *, regime: Regime | None = None
    ) -> CycleResult:
        result = CycleResult(cycle_id=new_id())
        features: dict[str, Features] = {}
        candidates: list[tuple[Signal, Instrument]] = []
        for instrument in universe:
            found = self._signals(instrument, result)
            if found is None:
                continue
            f, signals = found
            features[instrument.key] = f
            for signal in signals:
                self._sink.emit(SignalGenerated(signal=signal), source="decision",
                                cycle_id=result.cycle_id)  # fmt: skip
                result.signals.append(signal)
                if signal.is_shadow:
                    self._dispose_all(signal, Disposition.SHADOW_STRATEGY, "shadow strategy")
                    continue
                if not regime_allows(signal.strategy, regime, self.config.regime_gates):
                    result.skipped[signal.signal_id] = f"regime {regime}"
                    self._dispose_all(signal, Disposition.REGIME_GATED, f"regime {regime}")
                    continue
                candidates.append((signal, instrument))
        if not candidates:
            return result

        quotes = await self._requote({i.key for _, i in candidates})
        for book in self.books.values():
            proposals: list[Proposal] = []
            for signal, instrument in candidates:
                out = self._propose(book.book_id, signal, instrument, features, quotes)
                if isinstance(out, Skipped):
                    result.skipped[signal.signal_id] = out.reason
                    self._dispose(book.book_id, signal, Disposition.POLICY_SKIPPED, out.reason)
                else:
                    proposals.append(out)
            result.proposals.extend(proposals)
            ranked = sorted(proposals, key=_rank)
            for proposal in ranked:
                f = features[proposal.intent.instrument.key]
                await self._submit(book, proposal, f, result)
        return result

    def _propose(
        self,
        book_id: str,
        signal: Signal,
        instrument: Instrument,
        features: Mapping[str, Features],
        quotes: Mapping[str, Quote],
    ) -> Proposal | Skipped:
        series = self._market.daily(instrument.key)
        close = Decimal(str(series.last_close)) if series is not None else None
        quote = quotes.get(instrument.key)
        f = features[instrument.key]
        return self._policy.propose(
            signal,
            instrument,
            book_id=book_id,
            price=close or Decimal(0),
            arrival_price=Decimal(str(quote.ltp)) if quote is not None else None,
            atr=Decimal(str(f.atr_14)) if f.atr_14 else None,
            decision_ts=self._clock.now(),
            sink=self._sink,
        )

    async def _submit(
        self, book: BookTarget, proposal: Proposal, f: Features, result: CycleResult
    ) -> None:
        signal, intent = proposal.signal, proposal.intent
        if await self._vetoed(book, proposal, f):
            result.vetoed.append(proposal)
            self._dispose(book.book_id, signal, Disposition.VETOED, "advisor veto")
            return
        book.exits.expect_entry(intent, atr=proposal.atr, signal_bar_date=signal.bar_date)
        submitted = await book.oms.submit(intent)
        result.submitted.append((proposal, submitted))
        coid = submitted.order.client_order_id if submitted.order is not None else None
        self._dispose(book.book_id, signal, _DISPOSITIONS[submitted.status],
                      submitted.message[:300], coid)  # fmt: skip
        logger.info("%s %s %s: %s %s", book.book_id, intent.strategy, intent.instrument.symbol,
                    submitted.status, submitted.message)  # fmt: skip

    def _dispose(
        self,
        book_id: str,
        signal: Signal,
        disposition: Disposition,
        detail: str,
        client_order_id: str | None = None,
    ) -> None:
        self._sink.emit(
            SignalDisposition(signal_id=signal.signal_id, decision_id=signal.decision_id,
                              book_id=book_id, instrument_key=signal.instrument_key,
                              strategy=signal.strategy, disposition=disposition, detail=detail,
                              client_order_id=client_order_id),
            source="decision",
        )  # fmt: skip

    def _dispose_all(self, signal: Signal, disposition: Disposition, detail: str) -> None:
        for book_id in self.books:
            self._dispose(book_id, signal, disposition, detail)

    # -- steps ---------------------------------------------------------------------------------

    def _signals(
        self, instrument: Instrument, result: CycleResult
    ) -> tuple[Features, list[Signal]] | None:
        key = instrument.key
        series = self._market.daily(key)
        if series is None:
            result.skipped[key] = "no_history"
            return None
        if self._market.is_lagging(key):
            result.skipped[key] = "lagging"  # never trade on a series missing its newest close
            return None
        try:
            f = compute_features(series.adjusted(), key)
            signals = generate_signals(
                f,
                enabled=self.config.enabled,
                shadow=self.config.shadow,
                decision_id=new_id(),
                generated_at=self._clock.now(),
                stop_atr_mult=self._policy.config.k_stop_atr,
                target_atr_mult=self._policy.config.k_target_atr,
                strategies=self._strategies,
            )
        except Exception as exc:  # one bad series never stops the cycle
            logger.exception("features/signals failed for %s", key)
            result.skipped[key] = f"error: {type(exc).__name__}"
            self._sink.emit(
                Alert(level="WARNING", key="decision_symbol_failed",
                      message=f"{key}: {type(exc).__name__}: {exc}"),
                source="decision",
            )  # fmt: skip
            return None
        return f, signals

    async def _requote(self, keys: Collection[str]) -> Mapping[str, Quote]:
        try:
            return await self._market.requote(keys)
        except Exception as exc:  # the RiskEngine blocks on stale quotes; just say why
            logger.exception("re-quote failed")
            self._sink.emit(
                Alert(level="WARNING", key="decision_requote_failed",
                      message=f"{type(exc).__name__}: {exc}"),
                source="decision",
            )  # fmt: skip
            return {}

    async def _vetoed(self, book: BookTarget, proposal: Proposal, features: Features) -> bool:
        if book.advisor is None:
            return False
        try:
            verdict = await book.advisor.review(proposal, features)
        except Exception:  # an advisor failure never blocks or approves anything: ABSTAIN
            logger.exception("advisor failed on %s", proposal.intent.intent_id)
            return False
        return verdict is Verdict.VETO


_DISPOSITIONS = {
    "SUBMITTED": Disposition.SUBMITTED,
    "DUPLICATE": Disposition.SUBMITTED,
    "BLOCKED": Disposition.RISK_REJECTED,
    "REJECTED": Disposition.BROKER_REJECTED,
    "UNKNOWN": Disposition.UNKNOWN,
}


def _rank(p: Proposal) -> tuple[float, str, str]:
    return (-p.signal.agreement_score, p.intent.instrument.key, p.intent.strategy)
