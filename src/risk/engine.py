"""
The RiskEngine (plan M4.1; audit §L.1-§L.3): the single, binding pre-trade decision for every
order - opens, increases, reductions, closes, flattens and operator orders.

* Orders that **increase** risk fail **closed**: any BLOCK rejects, and a check that throws is a
  ``SYS_CHECK_ERROR`` BLOCK.
* Orders that **reduce** risk fail **open**: only the checks that apply to reductions run, and a
  check that throws is logged and allowed.
* **Sizing is a RESIZE step**: the approved quantity is the minimum of every cap (risk per trade
  over the stop distance, notional %/INR, % of ADV, gross/sector/heat/cash room, strategy
  allocation), floored to the lot. Zero shares means REJECTED.
* :meth:`evaluate_batch` reserves capacity as it approves, so proposals in one decision batch
  cannot all spend the same headroom.
* Every decision is a complete :class:`~src.domain.types.RiskDecision` record (reasons, checks run,
  limits hash, snapshot summary, kill state) for the caller to persist **before** routing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from src.config.limits import RiskLimits
from src.domain.ids import client_order_id
from src.domain.types import (
    CheckOutcome,
    OrderIntent,
    ReasonCode,
    RiskCheckResult,
    RiskDecision,
    RiskOutcome,
)
from src.risk.checks import order, portfolio, strategy, system
from src.risk.checks.base import RiskCheck
from src.risk.snapshot import Reservations, RiskContext, RiskSnapshot

logger = logging.getLogger(__name__)

ENGINE_VERSION = "risk-1"
DEFAULT_CHECKS: tuple[RiskCheck, ...] = (
    *system.CHECKS,
    *strategy.CHECKS,
    *portfolio.CHECKS,
    *order.CHECKS,
)
_HALT_CODES = frozenset(
    {ReasonCode.SYS_KILL_GLOBAL, ReasonCode.SYS_KILL_BROKER, ReasonCode.STR_HALTED}
)


@dataclass(frozen=True)
class Evaluation:
    decision: RiskDecision
    context: RiskContext

    @property
    def approved_qty(self) -> int:
        return self.decision.qty_approved


class RiskEngine:
    def __init__(self, limits: RiskLimits, checks: Sequence[RiskCheck] = DEFAULT_CHECKS) -> None:
        self.limits = limits
        self._checks = tuple(checks)
        self._limits_hash = limits.limits_hash()

    def evaluate(
        self,
        intent: OrderIntent,
        snapshot: RiskSnapshot,
        *,
        quantity: int | None = None,
        reserved: Reservations | None = None,
    ) -> Evaluation:
        requested = quantity if quantity is not None else intent.quantity
        ctx = RiskContext(intent, snapshot, self.limits, reserved or Reservations(), requested)
        results: list[RiskCheckResult] = []
        ran: list[str] = []
        for check in self._checks:
            if intent.kind not in check.applies_to:
                continue
            ran.append(check.code.value)
            try:
                result = check.evaluate(ctx)
            except Exception as exc:  # a broken check never approves a risk increase
                logger.exception("risk check %s failed", check.code)
                result = RiskCheckResult(
                    code=ReasonCode.SYS_CHECK_ERROR,
                    level=ReasonCode.SYS_CHECK_ERROR.level,
                    outcome=CheckOutcome.BLOCK if ctx.opening else CheckOutcome.ALLOW,
                    message=f"{check.code} raised {type(exc).__name__}: {exc}",
                )
            if result is not None:
                results.append(result)
        return Evaluation(self._decide(ctx, results, ran), ctx)

    def evaluate_batch(
        self, intents: Sequence[OrderIntent], snapshot: RiskSnapshot
    ) -> list[Evaluation]:
        """Evaluate in order, reserving each approval's capacity for the ones that follow."""
        reserved = Reservations()
        out = []
        for intent in intents:
            evaluation = self.evaluate(intent, snapshot, reserved=reserved)
            if evaluation.approved_qty > 0:
                reserved.add(evaluation.context, evaluation.approved_qty)
            out.append(evaluation)
        return out

    def preview(self, intent: OrderIntent, snapshot: RiskSnapshot) -> RiskDecision:
        """The same decision, for explanations in the UI. Only the OMS's decision is binding."""
        return self.evaluate(intent, snapshot).decision

    # -- the decision record -----------------------------------------------------------------------

    def _decide(
        self, ctx: RiskContext, results: list[RiskCheckResult], ran: list[str]
    ) -> RiskDecision:
        blocks = [r for r in results if r.outcome is CheckOutcome.BLOCK]
        caps = [r for r in results if r.outcome is CheckOutcome.RESIZE]
        lot = ctx.instrument.lot_size
        cap_values = [c.max_qty for c in caps if c.max_qty is not None]
        smallest = min(cap_values) if cap_values else None
        qty = ctx.requested_qty
        capped = smallest is not None and (qty is None or smallest < qty)
        if capped:
            qty = smallest
        if qty is not None:
            qty -= qty % lot
        # The cap(s) that set the size: recorded even when the engine did the sizing itself.
        binding = [c for c in caps if c.max_qty == smallest] if capped else []

        if blocks:
            outcome = (
                RiskOutcome.HALTED
                if any(b.code in _HALT_CODES for b in blocks)
                else RiskOutcome.REJECTED
            )
            approved, reasons = 0, blocks
        elif qty is None or qty <= 0:
            outcome, approved = RiskOutcome.REJECTED, 0
            reasons = caps or [
                RiskCheckResult(
                    code=ReasonCode.ORD_QTY_NONPOS,
                    level=ReasonCode.ORD_QTY_NONPOS.level,
                    outcome=CheckOutcome.BLOCK,
                    message="nothing sized the order",
                )
            ]
        else:
            approved = qty
            requested = ctx.requested_qty
            outcome = (
                RiskOutcome.RESIZED
                if requested is not None and approved < requested
                else RiskOutcome.APPROVED
            )
            reasons = binding
        informational = [r for r in results if r.outcome is CheckOutcome.ALLOW]

        price = ctx.price or ctx.intent.decision_price
        distance = ctx.stop_distance
        risk = distance * approved if distance is not None and distance > 0 else Decimal(0)
        equity = ctx.snapshot.equity
        quote = ctx.facts.quote
        intent = ctx.intent
        return RiskDecision(
            decision_id=intent.decision_id,
            intent_id=intent.intent_id,
            client_order_id=client_order_id(intent.intent_id),
            book_id=intent.book_id,
            strategy=intent.strategy,
            signal_id=intent.signal_id,
            instrument_key=intent.instrument.key,
            side=intent.side,
            product=intent.product,
            kind=intent.kind,
            source=intent.source,
            qty_requested=ctx.requested_qty
            if ctx.requested_qty and ctx.requested_qty > 0
            else None,
            qty_approved=approved,
            ref_price=ctx.price,
            stop_price=intent.stop_price,
            target_price=intent.target_price,
            outcome=outcome,
            notional=price * approved,
            risk_amount=risk,
            risk_pct_equity=float(risk / equity) if equity > 0 else 0.0,
            reasons=tuple(reasons) + tuple(informational),
            checks_run=tuple(ran),
            limits_hash=self._limits_hash,
            snapshot=ctx.snapshot.summary(
                None if quote is None else quote.age_seconds(ctx.snapshot.now),
                ctx.facts.data_source,
            ),
            kill_state=ctx.snapshot.kill,
            engine_version=ENGINE_VERSION,
            evaluated_at=ctx.snapshot.now,
        )
