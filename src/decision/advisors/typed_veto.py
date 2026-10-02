"""
Book B's typed veto (plan M7.9): the decision-model cascade (Laya → Jev) reviews a proposal.

* **Verbalisation** - numbers become categories deterministically (``rsi_14=78`` → "RSI
  overbought", ATR/price → "volatility high"), because typed decision models read words, not
  feature vectors, and the categories are reproducible.
* **State** - instrument, strategy and side; the categorical features; the regime label; and the
  instrument's ``TypedEvent``s from the last 10 sessions, published before now. Built by
  :func:`veto_state`, whose inputs are the proposal's public facts only - no positions or P&L.
* **Questions** - ``veto`` (noul) and ``concern`` (choice).
* **Verdict** - VETO when the calibrated P(veto) ≥ ``threshold`` (default 0.6), else APPROVE;
  any failure or abstention → ABSTAIN, so the deterministic decision stands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from src.decision_models.base import Question
from src.decision_models.cascade import CascadeResult
from src.decision_models.tasks.announcements import DecisionCascade
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock
from src.domain.events import AdvisorFallback, AdvisorRequested
from src.domain.sink import EventSink
from src.domain.types import (
    AdvisorKind,
    AdvisorVerdict,
    Regime,
    TypedEvent,
    Verdict,
    VerdictReason,
)
from src.features.technical import Features
from src.risk.events import shift_sessions
from src.strategies.policy import Proposal
from src.utils.market_time import IST

TASK = "typed_veto"
KIND = AdvisorKind.TYPED_VETO
LOOKBACK_SESSIONS = 10
QUESTIONS: Mapping[str, Question] = {
    "veto": Question(
        "noul",
        "Is there a concrete adverse event or condition in the context that makes opening a "
        "long position in this instrument today imprudent?",
        {"false": "no concrete adverse event or condition", "true": "a concrete adverse event or "
         "condition is present"},
    ),
    "concern": Question(
        "choice", "What is the main concern about opening this long position?",
        {"none": "no material concern", "event_risk": "a pending or recent corporate event",
         "trend_against": "the market or stock trend is against a long",
         "overextended": "the price has already moved too far", "liquidity": "thin liquidity",
         "other": "another concern"},
    ),
}  # fmt: skip


def verbalise(f: Features) -> dict[str, str]:
    """Deterministic categories for the decision model (missing inputs are left out)."""
    out: dict[str, str] = {}
    if f.rsi_14 is not None:
        out["rsi"] = ("RSI overbought" if f.rsi_14 >= 70 else "RSI oversold" if f.rsi_14 <= 30
                      else "RSI strong" if f.rsi_14 >= 60 else "RSI weak" if f.rsi_14 <= 40
                      else "RSI neutral")  # fmt: skip
    atr_pct = f.atr_pct
    if atr_pct is not None:
        out["volatility"] = ("volatility high" if atr_pct >= 0.03 else "volatility low"
                             if atr_pct <= 0.012 else "volatility normal")  # fmt: skip
    if f.adx_14 is not None:
        out["trend_strength"] = ("strong trend" if f.adx_14 >= 30 else "weak trend"
                                 if f.adx_14 < 20 else "moderate trend")  # fmt: skip
    for period, label in ((50, "50-day"), (200, "200-day")):
        average = f.sma.get(period)
        if average:
            out[f"vs_{period}d"] = (
                f"price {'above' if f.close >= average else 'below'} its {label} average"
            )
    if f.bb_percent is not None:
        out["bands"] = ("price above the upper Bollinger band" if f.bb_percent > 1
                        else "price below the lower Bollinger band" if f.bb_percent < 0
                        else "price inside the Bollinger bands")  # fmt: skip
    if f.prev_close:
        move = f.close / f.prev_close - 1
        out["last_session"] = ("sharp rise last session" if move >= 0.04 else "sharp fall last "
                               "session" if move <= -0.04 else "ordinary last session")  # fmt: skip
    if f.adv20_inr is not None:
        out["liquidity"] = ("liquidity high" if f.adv20_inr >= 5e9 else "liquidity low"
                            if f.adv20_inr < 2e8 else "liquidity adequate")  # fmt: skip
    return out


def describe_event(event: TypedEvent) -> str:
    parts = [event.published_at.astimezone(IST).date().isoformat()]
    if event.announcement_type is not None:
        parts.append(event.announcement_type.value.replace("_", " "))
    if event.direction is not None:
        parts.append(event.direction.value)
    if event.materiality is not None:
        parts.append(event.materiality.value)
    when = event.extra.get("event_date")
    if isinstance(when, str):
        parts.append(f"scheduled {when}")
    return f"{' | '.join(parts)}: {event.title[:200]}"


def veto_state(
    proposal: Proposal,
    features: Features,
    regime: Regime | None,
    events: Sequence[TypedEvent],
) -> dict[str, str]:
    """The decision-model state: public facts about the proposal only."""
    intent = proposal.intent
    state = {
        "instrument": f"{intent.instrument.name or intent.instrument.symbol} "
                      f"({intent.instrument.symbol}, {intent.instrument.sector or 'sector unknown'})",
        "proposal": f"open a {intent.side.value} position, strategy {intent.strategy}",
        "market_regime": regime.value.replace("_", " ") if regime else "unknown",
        "features": "; ".join(verbalise(features).values()) or "unknown",
        "events": "\n".join(describe_event(e) for e in events) or "no announcements",
    }  # fmt: skip
    return state


class TypedVetoAdvisor:
    def __init__(
        self,
        *,
        cascade: DecisionCascade,
        sink: EventSink,
        clock: Clock,
        calendar: NSECalendar,
        book_id: str,
        events: Callable[[str], Sequence[TypedEvent]],
        regime: Callable[[], Regime | None],
        threshold: float = 0.6,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        self._cascade = cascade
        self._sink = sink
        self._clock = clock
        self._calendar = calendar
        self.book_id = book_id
        self._events = events
        self._regime = regime
        self.threshold = threshold

    def recent_events(self, instrument_key: str, now: datetime) -> list[TypedEvent]:
        today = now.astimezone(IST).date()
        # The 10 most recent sessions, today included.
        since = shift_sessions(self._calendar, today, -(LOOKBACK_SESSIONS - 1))
        recent = [e for e in self._events(instrument_key)
                  if e.published_at <= now and e.published_at.astimezone(IST).date() >= since]  # fmt: skip
        return sorted(recent, key=lambda e: e.published_at)

    async def review(self, proposal: Proposal, features: Features) -> Verdict:
        decision_id = proposal.intent.decision_id
        self._sink.emit(AdvisorRequested(decision_id=decision_id, book_id=self.book_id,
                                         advisor=KIND, signal_id=proposal.signal.signal_id),
                        source="advisor")  # fmt: skip
        events = self.recent_events(proposal.intent.instrument.key, self._clock.now())
        state = veto_state(proposal, features, self._regime(), events)
        try:
            result = await self._cascade.decide(TASK, state, QUESTIONS, decision_id=decision_id,
                                                book_id=self.book_id)  # fmt: skip
        except Exception as exc:  # the cascade never raises, but an advisor never may either
            return self._abstain(decision_id, "cascade_error", f"{type(exc).__name__}: {exc}")
        return self._verdict(decision_id, result)

    def _verdict(self, decision_id: str, result: CascadeResult) -> Verdict:
        veto = result.answers.get("veto")
        if veto is None or veto.p_true is None:
            reason = result.abstained.get("veto", "no_answer")
            return self._abstain(decision_id, f"veto_{reason}", None)
        p_veto = veto.p_true
        verdict = Verdict.VETO if p_veto >= self.threshold else Verdict.APPROVE
        concern = result.answers.get("concern")
        reasons: tuple[VerdictReason, ...] = ()
        if concern is not None and isinstance(concern.value, str) and concern.value != "none":
            reasons = (VerdictReason(claim=f"main concern: {concern.value}",
                                     evidence_ref=f"concern:{concern.value}"),)  # fmt: skip
        model, _, checkpoint = veto.model.partition(":")
        self._sink.emit(
            AdvisorVerdict(decision_id=decision_id, book_id=self.book_id, advisor=KIND,
                           verdict=verdict, confidence=round(p_veto, 4), reasons=reasons,
                           provider=model or None, model=checkpoint or None),
            source="advisor",
        )  # fmt: skip
        return verdict

    def _abstain(self, decision_id: str, reason: str, error: str | None) -> Verdict:
        self._sink.emit(AdvisorFallback(decision_id=decision_id, book_id=self.book_id,
                                        advisor=KIND, reason=reason, error=error),
                        source="advisor")  # fmt: skip
        self._sink.emit(AdvisorVerdict(decision_id=decision_id, book_id=self.book_id,
                                       advisor=KIND, verdict=Verdict.ABSTAIN,
                                       abstain_reason=reason), source="advisor")  # fmt: skip
        return Verdict.ABSTAIN
