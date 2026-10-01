"""The risk-check contract (audit §L.3) and helpers to build results."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from src.domain.types import CheckOutcome, IntentKind, ReasonCode, RiskCheckResult
from src.risk.snapshot import RiskContext

OPENS = frozenset({IntentKind.OPEN, IntentKind.INCREASE})
REDUCES = frozenset({IntentKind.REDUCE, IntentKind.CLOSE, IntentKind.FLATTEN})
ALL_KINDS = OPENS | REDUCES


class RiskCheck(Protocol):
    @property
    def code(self) -> ReasonCode: ...

    @property
    def applies_to(self) -> frozenset[IntentKind]:
        """Entry checks never apply to REDUCE/CLOSE/FLATTEN."""
        ...

    def evaluate(self, ctx: RiskContext) -> RiskCheckResult | None:
        """None when the check passes silently; a result to BLOCK, RESIZE or annotate."""
        ...


@dataclass(frozen=True)
class Check:
    code: ReasonCode
    applies_to: frozenset[IntentKind]
    fn: Callable[[RiskContext], RiskCheckResult | None]

    def evaluate(self, ctx: RiskContext) -> RiskCheckResult | None:
        return self.fn(ctx)


def block(
    code: ReasonCode,
    message: str,
    *,
    observed: float | str | None = None,
    limit: float | str | None = None,
) -> RiskCheckResult:
    return RiskCheckResult(
        code=code,
        level=code.level,
        outcome=CheckOutcome.BLOCK,
        observed=observed,
        limit=limit,
        message=message,
    )


def resize(
    code: ReasonCode,
    max_qty: int,
    message: str,
    *,
    observed: float | str | None = None,
    limit: float | str | None = None,
) -> RiskCheckResult:
    return RiskCheckResult(
        code=code,
        level=code.level,
        outcome=CheckOutcome.RESIZE,
        observed=observed,
        limit=limit,
        message=message,
        max_qty=max(0, max_qty),
    )


def info(code: ReasonCode, message: str) -> RiskCheckResult:
    """An ALLOW that is still recorded on the decision (e.g. ``SYS_LLM_DEGRADED``)."""
    return RiskCheckResult(code=code, level=code.level, outcome=CheckOutcome.ALLOW, message=message)


def shares(amount: Decimal, per_share: Decimal) -> int:
    """Whole shares that ``amount`` buys at ``per_share`` (0 when it buys none)."""
    if per_share <= 0 or amount <= 0:
        return 0
    return int((amount / per_share).to_integral_value(rounding=ROUND_FLOOR))


def f(value: Decimal | float | int) -> float:
    """A rounded float for the observed/limit columns of a check result."""
    return round(float(value), 4)
