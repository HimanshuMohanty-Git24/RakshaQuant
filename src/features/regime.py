"""
The deterministic NIFTY 50 regime (plan M5.3; audit §I.1, F-19).

Computed **once per day, pre-open**, from settled ``^NSEI`` daily bars:

* ``VOLATILE`` when the 20-day realised volatility is at or above its ``vol_high_pct``
  percentile over the trailing year;
* else ``TRENDING_UP`` / ``TRENDING_DOWN`` when ADX(14) ≥ ``adx_trend`` (direction from ±DI);
* else ``RANGING``.

The confirmed label changes only after the new raw label holds for ``confirm`` consecutive bars
(2-confirmation hysteresis), so one noisy day cannot flip it. It is recomputed from the bars each
time (no hidden state), so a restart gives the same answer.

The regime is categorical **context** for advisors and an **optional** per-strategy entry gate
(:func:`regime_allows`; empty config = no gating). It is never used for exits (the legacy LLM
regime's exits caused churn, audit F-19).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator

from src.domain.events import RegimeComputed
from src.domain.types import Regime
from src.features.technical import as_date

TRADING_DAYS = 252


@dataclass(frozen=True)
class RegimeConfig:
    adx_window: int = 14
    adx_trend: float = 25.0
    vol_window: int = 20
    vol_lookback: int = TRADING_DAYS
    vol_high_pct: float = 0.80
    confirm: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.vol_high_pct < 1:
            raise ValueError("vol_high_pct must be in (0, 1)")
        if self.confirm < 1 or self.adx_window < 2 or self.vol_window < 2:
            raise ValueError("confirm >= 1 and windows >= 2 are required")
        if self.vol_lookback < self.vol_window:
            raise ValueError("vol_lookback must cover at least one vol window")


@dataclass(frozen=True)
class RegimeReading:
    bar_date: date
    label: Regime
    raw: Regime
    changed: bool
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    vol_annualized: float | None
    vol_percentile: float | None

    def event(self, session_date: date) -> RegimeComputed:
        return RegimeComputed(
            session_date=session_date,
            bar_date=self.bar_date,
            label=self.label,
            raw=self.raw,
            changed=self.changed,
            adx=self.adx,
            plus_di=self.plus_di,
            minus_di=self.minus_di,
            vol_annualized=self.vol_annualized,
            vol_percentile=self.vol_percentile,
        )


def classify(
    adx: float | None,
    plus_di: float | None,
    minus_di: float | None,
    vol_percentile: float | None,
    config: RegimeConfig,
) -> Regime | None:
    """One bar's raw label (None while the indicators are still warming up)."""
    if vol_percentile is not None and vol_percentile >= config.vol_high_pct:
        return Regime.VOLATILE
    if adx is None or plus_di is None or minus_di is None:
        return None
    if adx >= config.adx_trend:
        return Regime.TRENDING_UP if plus_di >= minus_di else Regime.TRENDING_DOWN
    return Regime.RANGING


def compute_regime(frame: pd.DataFrame, config: RegimeConfig | None = None) -> RegimeReading:
    """The regime as of the last bar of ``frame`` (settled ^NSEI daily OHLC, oldest first)."""
    cfg = config or RegimeConfig()
    df = frame[["high", "low", "close"]].astype(float)
    n = len(df)
    if n < 2 * cfg.adx_window:
        raise ValueError(f"need at least {2 * cfg.adx_window} bars, got {n}")
    ind = ADXIndicator(df["high"], df["low"], df["close"], window=cfg.adx_window)
    adx, plus, minus = ind.adx(), ind.adx_pos(), ind.adx_neg()
    warm = 2 * cfg.adx_window - 1  # ta reports 0 (not NaN) before this bar
    log_ret = np.log(df["close"] / df["close"].shift(1))
    vol = log_ret.rolling(cfg.vol_window).std(ddof=1) * math.sqrt(TRADING_DAYS)
    pct = vol.rolling(cfg.vol_lookback, min_periods=cfg.vol_window).rank(pct=True)

    raws = [
        classify(
            _num(adx.iloc[i]) if i >= warm else None,
            _num(plus.iloc[i]) if i >= warm else None,
            _num(minus.iloc[i]) if i >= warm else None,
            _num(pct.iloc[i]),
            cfg,
        )
        for i in range(n)
    ]
    raw = raws[-1]
    confirmed, changed = hysteresis(raws, cfg.confirm)
    if confirmed is None or raw is None:
        raise ValueError("not enough history to classify the last bar")
    last = n - 1
    return RegimeReading(
        bar_date=as_date(df.index[last]),
        label=confirmed,
        raw=raw,
        changed=changed,
        adx=_num(adx.iloc[last]),
        plus_di=_num(plus.iloc[last]),
        minus_di=_num(minus.iloc[last]),
        vol_annualized=_num(vol.iloc[last]),
        vol_percentile=_num(pct.iloc[last]),
    )


def hysteresis(raws: Sequence[Regime | None], confirm: int) -> tuple[Regime | None, bool]:
    """The confirmed label after ``raws`` (oldest first), and whether the last bar changed it.
    A new label is confirmed once it holds for ``confirm`` consecutive classified bars."""
    confirmed: Regime | None = None
    candidate: Regime | None = None
    streak = 0
    changed = False
    for raw in raws:
        changed = False
        if raw is None:
            continue
        if confirmed is None:
            confirmed, changed = raw, True
        elif raw == confirmed:
            candidate, streak = None, 0
        else:
            streak = streak + 1 if raw == candidate else 1
            candidate = raw
            if streak >= confirm:
                confirmed, candidate, streak, changed = raw, None, 0, True
    return confirmed, changed


def regime_allows(
    strategy: str, regime: Regime | None, gates: Mapping[str, frozenset[Regime]]
) -> bool:
    """The optional entry gate: a strategy listed in ``gates`` trades only in its regimes (and
    not at all while the regime is unknown); unlisted strategies are never gated."""
    allowed = gates.get(strategy)
    if allowed is None:
        return True
    return regime is not None and regime in allowed


def _num(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(out, 6) if math.isfinite(out) else None
