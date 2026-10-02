"""Event-calendar checks (plan M7.8; audit §J.2): entries are blocked in windows built by
:mod:`src.risk.events` from classified announcements. They never apply to exits."""

from __future__ import annotations

from collections.abc import Callable

from src.domain.types import ReasonCode, RiskCheckResult
from src.risk.checks.base import OPENS, Check, block
from src.risk.snapshot import RiskContext
from src.utils.market_time import IST

R = ReasonCode


def _window(code: ReasonCode) -> Callable[[RiskContext], RiskCheckResult | None]:
    def check(ctx: RiskContext) -> RiskCheckResult | None:
        today = ctx.snapshot.now.astimezone(IST).date()
        for b in ctx.snapshot.event_blocks.get(ctx.instrument.key, ()):
            if b.code is code and b.covers(today):
                return block(code, f"{b.reason} (blocked {b.start} to {b.end})",
                             observed=b.event_id, limit=f"{b.start}..{b.end}")  # fmt: skip
        return None

    return check


CHECKS = (
    Check(R.EVT_RESULTS_WINDOW, OPENS, _window(R.EVT_RESULTS_WINDOW)),
    Check(R.EVT_ADVERSE_MAJOR, OPENS, _window(R.EVT_ADVERSE_MAJOR)),
)
