"""
Paired books (plan M8.1-8.2): the experiment's books → the engine's per-book stacks and advisors.

The multi-book mechanics live in the engine (:mod:`src.engine.runner`: one stack per book) and the
decision engine (one proposal per book through the book's own advisor, RiskGate and OMS, with the
``decision_id`` shared). This module turns ``experiment.yaml`` into that configuration:

* A ``advisor: none`` - the deterministic decision stands.
* B ``advisor: typed_veto`` - the decision-model cascade (Laya → Jev), VETO iff calibrated
  P(veto) ≥ ``threshold``.
* C ``advisor: llm_veto`` - the LLM router's role (``veto``), VETO only with grounded evidence and
  confidence ≥ ``threshold``.

When an advisor cannot run (no decision model installed or keyed, the LLM role switched off) the
book gets an :class:`AbstainAdvisor`: every proposal is recorded as ABSTAIN with the reason, so the
book trades like A *and the report shows why* - it never silently becomes a copy of A.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from src.config.errors import ConfigError
from src.config.limits import RiskLimits
from src.decision.advisors.llm_veto import LLMVetoAdvisor
from src.decision.advisors.typed_veto import TypedVetoAdvisor
from src.decision.engine import Advisor, DecisionConfig
from src.decision_models.tasks.announcements import DecisionCascade
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock
from src.domain.events import AdvisorFallback, AdvisorRequested
from src.domain.sink import EventSink
from src.domain.types import AdvisorKind, AdvisorVerdict, Regime, TypedEvent, Verdict
from src.engine.lifecycle import LifecycleConfig
from src.engine.runner import EngineConfig
from src.evaluation.experiment import ExperimentConfig
from src.features.technical import Features
from src.llm.router import LLMRouter
from src.strategies.policy import Proposal, TradePolicyConfig

KINDS = {"typed_veto": AdvisorKind.TYPED_VETO, "llm_veto": AdvisorKind.LLM_VETO}


def engine_config(
    experiment: ExperimentConfig, *, environment: str, limits: RiskLimits, **extra: Any
) -> EngineConfig:
    """The engine configuration the experiment implies (``extra``: halt file, costs, ...)."""
    unlicensed = set(experiment.strategies.enabled) - set(limits.enabled_strategies)
    if unlicensed:
        raise ConfigError(f"experiment enables {sorted(unlicensed)} but RISK_ENABLED_STRATEGIES "
                          f"allows only {list(limits.enabled_strategies)}")  # fmt: skip
    start, end = experiment.window()
    p = experiment.policy
    return EngineConfig(
        environment=environment,
        books=experiment.book_ids,
        starting_cash=experiment.capital_inr,
        lifecycle=LifecycleConfig(entry_window_start=start, entry_window_end=end),
        policy=TradePolicyConfig(k_stop_atr=p.k_stop_atr, k_target_atr=p.k_target_atr,
                                 max_hold_days=p.max_hold_days, partial_at_r=p.partial_at_r),
        decision=DecisionConfig(book_id=experiment.book_ids[0],
                                enabled=experiment.strategies.enabled,
                                shadow=experiment.strategies.shadow),
        **extra,
    )  # fmt: skip


class AbstainAdvisor:
    """A book whose advisor is unavailable: every proposal is ABSTAIN, recorded with why."""

    def __init__(self, *, kind: AdvisorKind, book_id: str, reason: str, sink: EventSink) -> None:
        self.kind, self.book_id, self.reason = kind, book_id, reason
        self._sink = sink

    async def review(self, proposal: Proposal, features: Features) -> Verdict:
        decision_id = proposal.intent.decision_id
        self._sink.emit(AdvisorRequested(decision_id=decision_id, book_id=self.book_id,
                                         advisor=self.kind, signal_id=proposal.signal.signal_id),
                        source="advisor")  # fmt: skip
        self._sink.emit(AdvisorFallback(decision_id=decision_id, book_id=self.book_id,
                                        advisor=self.kind, reason=self.reason), source="advisor")  # fmt: skip
        self._sink.emit(AdvisorVerdict(decision_id=decision_id, book_id=self.book_id,
                                       advisor=self.kind, verdict=Verdict.ABSTAIN,
                                       abstain_reason=self.reason), source="advisor")  # fmt: skip
        return Verdict.ABSTAIN


def build_advisors(
    experiment: ExperimentConfig,
    *,
    sink: EventSink,
    clock: Clock,
    calendar: NSECalendar,
    cascade: DecisionCascade | None,
    router: LLMRouter | None,
    events: Callable[[str], Sequence[TypedEvent]],
    regime: Callable[[], Regime | None],
) -> dict[str, Advisor | None]:
    advisors: dict[str, Advisor | None] = {}
    for book_id, spec in experiment.books.items():
        if spec.advisor == "none":
            advisors[book_id] = None
        elif spec.advisor == "typed_veto":
            if cascade is None:
                advisors[book_id] = AbstainAdvisor(kind=KINDS["typed_veto"], book_id=book_id,
                                                   reason="no_decision_model", sink=sink)  # fmt: skip
            else:
                advisors[book_id] = TypedVetoAdvisor(
                    cascade=cascade, sink=sink, clock=clock, calendar=calendar, book_id=book_id,
                    events=events, regime=regime, threshold=spec.threshold or 0.6,
                )  # fmt: skip
        else:
            role = spec.role or "veto"
            config = router.roles.get(role) if router is not None else None
            if router is None or config is None or not config.enabled:
                advisors[book_id] = AbstainAdvisor(kind=KINDS["llm_veto"], book_id=book_id,
                                                   reason=f"llm_role_{role}_disabled", sink=sink)  # fmt: skip
            else:
                advisors[book_id] = LLMVetoAdvisor(router=router, sink=sink, book_id=book_id,
                                                   role=role, threshold=spec.threshold)  # fmt: skip
    return advisors
