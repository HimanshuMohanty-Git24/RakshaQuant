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
4. **Advisor** (per book; M8): may only veto. No advisor = the deterministic decision stands.
5. **Submit** through ``OMS.submit`` in descending ``agreement_score`` (the RiskEngine sizes,
   reserves capacity, and may reject), after telling the ExitManager the entry's ATR.

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
from src.domain.events import Alert, SignalGenerated
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
        oms: OMS,
        exits: ExitManager,
        policy: TradePolicy,
        clock: Clock,
        sink: EventSink,
        advisor: Advisor | None = None,
        strategies: Mapping[str, Strategy] | None = None,
    ) -> None:
        if oms.book_id != config.book_id:
            raise ValueError("the decision engine and its OMS must share the book id")
        self.config = config
        self._market = market
        self._oms = oms
        self._exits = exits
        self._policy = policy
        self._clock = clock
        self._sink = sink
        self._advisor = advisor
        self._strategies = strategies

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
                    continue
                if not regime_allows(signal.strategy, regime, self.config.regime_gates):
                    result.skipped[signal.signal_id] = f"regime {regime}"
                    continue
                candidates.append((signal, instrument))
        if not candidates:
            return result

        quotes = await self._requote({i.key for _, i in candidates})
        for signal, instrument in candidates:
            series = self._market.daily(instrument.key)
            close = Decimal(str(series.last_close)) if series is not None else None
            quote = quotes.get(instrument.key)
            f = features[instrument.key]
            out = self._policy.propose(
                signal,
                instrument,
                book_id=self.config.book_id,
                price=close or Decimal(0),
                arrival_price=Decimal(str(quote.ltp)) if quote is not None else None,
                atr=Decimal(str(f.atr_14)) if f.atr_14 else None,
                decision_ts=self._clock.now(),
                sink=self._sink,
            )
            if isinstance(out, Skipped):
                result.skipped[signal.signal_id] = out.reason
            else:
                result.proposals.append(out)

        ranked = sorted(result.proposals,
                        key=lambda p: (-p.signal.agreement_score, p.intent.instrument.key,
                                       p.intent.strategy))  # fmt: skip
        for proposal in ranked:
            if await self._vetoed(proposal, features[proposal.intent.instrument.key]):
                result.vetoed.append(proposal)
                continue
            intent = proposal.intent
            self._exits.expect_entry(intent, atr=proposal.atr,
                                     signal_bar_date=proposal.signal.bar_date)  # fmt: skip
            submitted = await self._oms.submit(intent)
            result.submitted.append((proposal, submitted))
            logger.info("%s %s: %s %s", intent.strategy, intent.instrument.symbol,
                        submitted.status, submitted.message)  # fmt: skip
        return result

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

    async def _vetoed(self, proposal: Proposal, features: Features) -> bool:
        if self._advisor is None:
            return False
        try:
            verdict = await self._advisor.review(proposal, features)
        except Exception:  # an advisor failure never blocks or approves anything: ABSTAIN
            logger.exception("advisor failed on %s", proposal.intent.intent_id)
            return False
        return verdict is Verdict.VETO
