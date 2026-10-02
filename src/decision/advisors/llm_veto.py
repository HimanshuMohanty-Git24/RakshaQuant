"""
Book C's LLM veto advisor (plan M6 roles, M8 books; audit §I.2, §J.2).

Veto-only and fail-safe:

* The input is the proposal's signal and settled-bar features (and, from M7, typed events) - no
  portfolio data - rendered into ``<data>`` blocks by ``veto_v1``.
* Any router failure (all providers down, timeout, budget, refusal, invalid output) → ABSTAIN,
  recorded as ``AdvisorFallback``: the deterministic decision stands, so with every provider down
  Book C trades exactly like Book A (rule 5).
* A reason whose ``evidence_ref`` names nothing in the input (a hallucination), a VETO with no
  reasons, or a VETO less confident than the book's ``threshold`` → ABSTAIN.
* The verdict can only remove a trade: the router's output has no size, price, stop or target,
  and an APPROVE never overrides the RiskEngine.
"""

from __future__ import annotations

from typing import Any

from src.domain.events import AdvisorFallback, AdvisorRequested
from src.domain.sink import EventSink
from src.domain.types import AdvisorKind, AdvisorVerdict, Verdict, VerdictReason
from src.features.technical import Features
from src.llm.prompts.base import PromptTemplate
from src.llm.prompts.evidence import unsupported
from src.llm.prompts.templates import VETO_V1, VetoOutput
from src.llm.router import LLMRouter
from src.strategies.policy import Proposal

KIND = AdvisorKind.LLM_VETO


def veto_input(proposal: Proposal, features: Features) -> dict[str, Any]:
    """What the veto sees: the proposal and its market context - never positions or P&L."""
    signal = proposal.signal
    return {
        "signal": {
            "instrument": proposal.intent.instrument.symbol,
            "sector": proposal.intent.instrument.sector,
            "strategy": signal.strategy,
            "side": signal.side.value,
            "bar_date": signal.bar_date.isoformat(),
            "agreement_score": signal.agreement_score,
            "reasons": {r.name: r.value for r in signal.reasons},
        },
        "features": features.as_dict(),
        "events": [],  # typed events arrive in M7
    }


class LLMVetoAdvisor:
    def __init__(
        self,
        *,
        router: LLMRouter,
        sink: EventSink,
        book_id: str,
        template: PromptTemplate = VETO_V1,
        role: str = "veto",
        threshold: float | None = None,
    ) -> None:
        if threshold is not None and not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        self._router = router
        self._sink = sink
        self.book_id = book_id
        self._template = template
        self._role = role
        self.threshold = threshold

    async def review(self, proposal: Proposal, features: Features) -> Verdict:
        decision_id = proposal.intent.decision_id
        data = veto_input(proposal, features)
        self._sink.emit(AdvisorRequested(decision_id=decision_id, book_id=self.book_id,
                                         advisor=KIND, signal_id=proposal.signal.signal_id),
                        source="advisor")  # fmt: skip
        result = await self._router.complete(
            self._role,
            self._template.render(data),
            VetoOutput,
            prompt_version=self._template.prompt_version,
            decision_id=decision_id,
            book_id=self.book_id,
        )
        if not result.ok or result.parsed is None:
            return self._abstain(decision_id, f"llm_{result.outcome}", result.error)
        out = result.parsed
        missing = unsupported([r.evidence_ref for r in out.reasons], data)
        if missing:
            return self._abstain(decision_id, "hallucinated_evidence", ", ".join(missing)[:300])
        if out.verdict == "VETO" and not out.reasons:
            return self._abstain(decision_id, "veto_without_evidence", None)
        if out.verdict == "VETO" and self.threshold is not None and out.confidence < self.threshold:
            return self._abstain(
                decision_id, "veto_below_threshold", f"confidence {out.confidence}"
            )
        verdict = Verdict(out.verdict)
        provider, _, model = (result.model or "").partition(":")
        self._sink.emit(
            AdvisorVerdict(
                decision_id=decision_id, book_id=self.book_id, advisor=KIND, verdict=verdict,
                confidence=out.confidence,
                reasons=tuple(VerdictReason(claim=r.claim, evidence_ref=r.evidence_ref)
                              for r in out.reasons if r.claim and r.evidence_ref),
                provider=provider or None, model=model or None,
                abstain_reason="model abstained" if verdict is Verdict.ABSTAIN else None,
            ),
            source="advisor",
        )  # fmt: skip
        return verdict

    def _abstain(self, decision_id: str, reason: str, error: str | None) -> Verdict:
        self._sink.emit(AdvisorFallback(decision_id=decision_id, book_id=self.book_id,
                                        advisor=KIND, reason=reason, error=error),
                        source="advisor")  # fmt: skip
        self._sink.emit(AdvisorVerdict(decision_id=decision_id, book_id=self.book_id,
                                       advisor=KIND, verdict=Verdict.ABSTAIN,
                                       abstain_reason=reason),
                        source="advisor")  # fmt: skip
        return Verdict.ABSTAIN
