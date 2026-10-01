"""
The strategy contract (plan M5.1).

A strategy looks at one instrument's :class:`~src.features.technical.Features` and returns a
:class:`Detection` (a side, its own base score, and structured reasons) or ``None``.
:func:`make_signal` turns a detection into the domain :class:`~src.domain.types.Signal`.

``agreement_score`` (formerly "confidence") blends the strategy's base score with how many
independent indicators agree with the side. It is a **score, not a probability**: it is not
calibrated until M8 fits it on outcomes. The RSI vote is **contrarian** for mean reversion (low
RSI supports a BUY) and **trend-confirming** for the other strategies (RSI above 50 supports a
BUY) - the legacy engine voted contrarian for all of them (audit F-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.domain.types import Side, Signal, SignalReason
from src.features.technical import Features

SCORE_CAP = 0.95


@dataclass(frozen=True)
class StrategyParams:
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    adx_trend: float = 25.0
    adx_strong: float = 40.0
    bb_squeeze: float = 0.10  # band width / middle


@dataclass(frozen=True)
class Detection:
    side: Side
    base_score: float  # the strategy's own strength ladder, in [0, 1]
    reasons: tuple[SignalReason, ...]


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def rsi_contrarian(self) -> bool:
        """True when a low RSI supports a BUY (mean reversion)."""
        ...

    def detect(self, f: Features) -> Detection | None: ...


def reason(name: str, value: float | str | bool | None = None, detail: str = "") -> SignalReason:
    if isinstance(value, float):
        value = round(value, 4)
    return SignalReason(name=name, value=value, detail=detail)


def agreement(f: Features, side: Side, *, rsi_contrarian: bool) -> float:
    """The fraction of independent indicators (RSI, MACD histogram, DI, price vs MA) that agree
    with ``side``; 0.5 when none is available."""
    buy = side is Side.BUY
    votes: list[bool] = []
    if f.rsi_14 is not None:
        supports_buy = f.rsi_14 < 50 if rsi_contrarian else f.rsi_14 > 50
        supports_sell = f.rsi_14 > 50 if rsi_contrarian else f.rsi_14 < 50
        votes.append(supports_buy if buy else supports_sell)
    if f.macd_hist is not None:
        votes.append(f.macd_hist > 0 if buy else f.macd_hist < 0)
    if f.plus_di_14 is not None and f.minus_di_14 is not None:
        votes.append(f.plus_di_14 > f.minus_di_14 if buy else f.minus_di_14 > f.plus_di_14)
    ref = f.ema.get(21) or f.sma.get(20)
    if ref is not None:
        votes.append(f.close > ref if buy else f.close < ref)
    return sum(votes) / len(votes) if votes else 0.5


def agreement_score(f: Features, detection: Detection, *, rsi_contrarian: bool) -> float:
    blended = 0.4 * detection.base_score + 0.6 * (
        0.35 + 0.55 * agreement(f, detection.side, rsi_contrarian=rsi_contrarian)
    )
    return round(min(SCORE_CAP, blended), 2)


def signal_id(strategy: str, instrument_key: str, f: Features) -> str:
    """Deterministic: the same strategy on the same settled bar is the same signal."""
    return f"{strategy}:{instrument_key}:{f.bar_date.isoformat()}"


def make_signal(
    strategy: Strategy,
    f: Features,
    detection: Detection,
    *,
    decision_id: str,
    generated_at: datetime,
    stop_atr_mult: float,
    target_atr_mult: float,
    is_shadow: bool,
) -> Signal:
    score = agreement_score(f, detection, rsi_contrarian=strategy.rsi_contrarian)
    return Signal(
        signal_id=signal_id(strategy.name, f.instrument_key, f),
        decision_id=decision_id,
        instrument_key=f.instrument_key,
        strategy=strategy.name,
        side=detection.side,
        bar_date=f.bar_date,
        agreement_score=score,
        stop_atr_mult=stop_atr_mult,
        target_atr_mult=target_atr_mult,
        is_shadow=is_shadow,
        reasons=(
            *detection.reasons,
            reason("base_score", detection.base_score),
            reason("close", f.close),
            reason("atr_14", f.atr_14),
        ),
        generated_at=generated_at,
    )
