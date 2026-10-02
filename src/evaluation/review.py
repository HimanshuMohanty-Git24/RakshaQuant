"""
The nightly post-trade review (plan M8.7; audit §J.2): role ``review`` (prompt ``review_v1``)
writes structured lessons for each trade closed that day, from the trade's full lineage - the
signal and its reasons, the risk decision, the book's advisor verdict, the counterfactual and the
day's regime. Each review is a ``TradeReview`` event stamped ``resolved_at`` (point in time).

* Lessons whose ``evidence_ref`` names nothing in the input are dropped (counted, not stored).
* A trade is reviewed once; a failed call leaves it for the next night.
* **Not injected anywhere during month 1** (audit F-20): no decision, risk or advisor code reads
  ``TradeReview`` - ``tests/test_review.py`` guards that.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from src.domain.clock import Clock
from src.domain.events import (
    Event,
    RegimeComputed,
    ReviewLesson,
    ShadowTradeClosed,
    SignalGenerated,
    TradeClosed,
    TradeReview,
)
from src.domain.sink import EventSink
from src.domain.types import AdvisorVerdict, RiskDecision
from src.llm.prompts.evidence import unsupported
from src.llm.prompts.templates import REVIEW_V1, ReviewOutput
from src.llm.router import LLMRouter
from src.store.event_store import EventStore

logger = logging.getLogger(__name__)
ROLE = "review"


def review_input(trade: TradeClosed, lineage: list[Event]) -> dict[str, Any]:
    """What the reviewer sees: the trade and its recorded lineage (no other book's data)."""
    data: dict[str, Any] = {"trade": {
        "book": trade.book_id, "instrument": trade.instrument_key, "strategy": trade.strategy,
        "side": trade.side.value, "quantity": trade.quantity,
        "entry_price": str(trade.entry_price), "exit_price": str(trade.exit_price),
        "entry_time": trade.entry_ts.isoformat(), "exit_time": trade.exit_ts.isoformat(),
        "exit_reason": trade.exit_reason, "net_pnl": str(trade.net_pnl),
        "charges": str(trade.charges),
    }}  # fmt: skip
    for event in lineage:
        p = event.payload
        if isinstance(p, SignalGenerated):
            data["signal"] = {"agreement_score": p.signal.agreement_score,
                              "bar_date": p.signal.bar_date.isoformat(),
                              "reasons": {r.name: r.value for r in p.signal.reasons}}  # fmt: skip
        elif isinstance(p, RiskDecision) and p.book_id == trade.book_id and "risk" not in data:
            data["risk"] = {"outcome": p.outcome.value, "qty_approved": p.qty_approved,
                            "reasons": [r.code.value for r in p.reasons]}  # fmt: skip
        elif isinstance(p, AdvisorVerdict) and p.book_id == trade.book_id:
            data["advisor"] = {"verdict": p.verdict.value, "advisor": p.advisor.value}
        elif isinstance(p, ShadowTradeClosed):
            data["counterfactual"] = {"exit_reason": p.exit_reason,
                                      "net_return_pct": p.net_return_pct}  # fmt: skip
    return data


async def review_day(
    store: EventStore, router: LLMRouter, day: date, *, clock: Clock, sink: EventSink
) -> list[TradeReview]:
    """Review every trade closed on ``day`` that has no review yet."""
    config = router.roles.get(ROLE)
    if config is None or not config.enabled:
        logger.info("nightly review skipped: LLM role %r is not configured", ROLE)
        return []
    done = {e.payload.trade_id for e in store.read(types=["TradeReview"])
            if isinstance(e.payload, TradeReview)}  # fmt: skip
    regime = next((e.payload for e in store.read(types=["RegimeComputed"], ist_date=day)
                   if isinstance(e.payload, RegimeComputed)), None)  # fmt: skip
    reviews: list[TradeReview] = []
    for event in store.read(types=["TradeClosed"], ist_date=day):
        trade = event.payload
        if not isinstance(trade, TradeClosed) or trade.trade_id in done:
            continue
        lineage = list(store.read(decision_id=trade.decision_id))
        data = review_input(trade, lineage)
        if regime is not None:
            data["market"] = {"regime": regime.label.value}
        result = await router.complete(ROLE, REVIEW_V1.render(data), ReviewOutput,
                                       prompt_version=REVIEW_V1.prompt_version,
                                       decision_id=trade.decision_id, book_id=trade.book_id)  # fmt: skip
        if not result.ok or result.parsed is None:
            logger.warning("no review for %s: %s", trade.trade_id, result.outcome)
            continue
        out = result.parsed
        grounded = [l for l in out.lessons if not unsupported([l.evidence_ref], data)]  # noqa: E741
        review = TradeReview(
            trade_id=trade.trade_id, book_id=trade.book_id, decision_id=trade.decision_id,
            instrument_key=trade.instrument_key, summary=out.summary,
            what_worked=tuple(out.what_worked), what_failed=tuple(out.what_failed),
            lessons=tuple(ReviewLesson(claim=l.claim, evidence_ref=l.evidence_ref)
                          for l in grounded if l.claim and l.evidence_ref),  # noqa: E741
            dropped_lessons=len(out.lessons) - len(grounded), model=result.model or "unknown",
            prompt_version=REVIEW_V1.prompt_version, resolved_at=clock.now(),
        )  # fmt: skip
        sink.emit(review, source="review")
        reviews.append(review)
    return reviews
