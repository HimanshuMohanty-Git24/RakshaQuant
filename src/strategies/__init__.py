"""
Strategies (plan M5.1): one module per strategy, all deterministic and backtestable.

:func:`generate_signals` runs the **enabled** strategies (their signals may trade) and the
**shadow** strategies (recorded and scored, never traded; plan D8) over one instrument's features.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from src.domain.types import Signal
from src.features.technical import Features
from src.strategies.base import Strategy, StrategyParams, make_signal
from src.strategies.breakout import Breakout
from src.strategies.mean_reversion import MeanReversion
from src.strategies.momentum import Momentum
from src.strategies.trend_following import TrendFollowing


def registry(params: StrategyParams | None = None) -> dict[str, Strategy]:
    p = params or StrategyParams()
    strategies: list[Strategy] = [Momentum(p), MeanReversion(p), Breakout(p), TrendFollowing(p)]
    return {s.name: s for s in strategies}


def generate_signals(
    f: Features,
    *,
    enabled: Iterable[str],
    shadow: Iterable[str],
    decision_id: str,
    generated_at: datetime,
    stop_atr_mult: float,
    target_atr_mult: float,
    strategies: Mapping[str, Strategy] | None = None,
) -> list[Signal]:
    """Signals for ``f``: enabled strategies first, then shadow ones (``is_shadow=True``)."""
    known = strategies if strategies is not None else registry()
    enabled_names = list(dict.fromkeys(enabled))
    shadow_names = [n for n in dict.fromkeys(shadow) if n not in enabled_names]
    unknown = [n for n in (*enabled_names, *shadow_names) if n not in known]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}")
    out: list[Signal] = []
    for name, is_shadow in [(n, False) for n in enabled_names] + [(n, True) for n in shadow_names]:
        strategy = known[name]
        detection = strategy.detect(f)
        if detection is not None:
            out.append(
                make_signal(
                    strategy,
                    f,
                    detection,
                    decision_id=decision_id,
                    generated_at=generated_at,
                    stop_atr_mult=stop_atr_mult,
                    target_atr_mult=target_atr_mult,
                    is_shadow=is_shadow,
                )
            )
    return out


__all__ = ["Strategy", "StrategyParams", "generate_signals", "registry"]
