"""Plan M5.3: the deterministic NIFTY regime with 2-confirmation hysteresis."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.domain.types import Regime
from src.features.regime import (
    RegimeConfig,
    classify,
    compute_regime,
    hysteresis,
    regime_allows,
)

U, D, RG, V = Regime.TRENDING_UP, Regime.TRENDING_DOWN, Regime.RANGING, Regime.VOLATILE
CFG = RegimeConfig()


def index(returns: np.ndarray, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 20_000 * np.exp(np.cumsum(returns))
    spread = np.abs(rng.normal(0, 0.004, len(close))) + 0.001
    frame = pd.DataFrame(
        {"open": close, "high": close * (1 + spread), "low": close * (1 - spread), "close": close},
        index=pd.bdate_range("2025-01-01", periods=len(close)),
    )
    return frame


def calm(n: int, drift: float = 0.0, sd: float = 0.006, seed: int = 1) -> np.ndarray:
    return np.random.default_rng(seed).normal(drift, sd, n)


def test_classify():
    assert classify(30, 25, 15, 0.5, CFG) is U
    assert classify(30, 15, 25, 0.5, CFG) is D
    assert classify(18, 25, 15, 0.5, CFG) is RG
    assert classify(30, 25, 15, 0.85, CFG) is V  # volatility overrides the trend
    assert classify(None, None, None, 0.9, CFG) is V
    assert classify(None, None, None, 0.5, CFG) is None


def test_hysteresis_needs_two_confirmations():
    assert hysteresis([RG, RG, U], 2) == (RG, False)  # one day is noise
    assert hysteresis([RG, RG, U, U], 2) == (U, True)
    assert hysteresis([RG, U, D, U, D], 2) == (RG, False)  # flip-flopping never confirms
    assert hysteresis([None, None, RG], 2) == (RG, True)  # the first classified bar seeds it
    assert hysteresis([RG, U, U, RG], 2) == (U, False)
    assert hysteresis([], 2) == (None, False)


def test_a_steady_rally_is_trending_up_and_a_fall_trending_down():
    up = compute_regime(index(calm(300, drift=0.004)))
    assert up.label is U and up.adx is not None and up.adx >= 25
    down = compute_regime(index(calm(300, drift=-0.004)))
    assert down.label is D


def test_a_sideways_oscillating_market_is_ranging():
    t = np.arange(301)
    noise = np.random.default_rng(3).normal(0, 1, 301) * np.linspace(0.004, 0.001, 301)
    level = np.log(20_000) + 0.01 * np.sin(2 * np.pi * t / 6) + noise  # choppy, calming
    reading = compute_regime(index(np.diff(level)))
    assert reading.adx is not None and reading.adx < 25
    assert reading.label is RG and reading.raw is RG


def test_a_volatility_spike_is_volatile_after_confirmation():
    returns = np.concatenate([calm(280, sd=0.005), calm(30, sd=0.03, seed=9)])
    reading = compute_regime(index(returns))
    assert reading.label is V and reading.vol_percentile is not None
    assert reading.vol_percentile >= 0.8 and reading.vol_annualized is not None


def test_it_is_deterministic_and_needs_history():
    frame = index(calm(300, drift=0.002))
    assert compute_regime(frame) == compute_regime(frame.copy())
    assert compute_regime(frame).bar_date == frame.index[-1].date()
    with pytest.raises(ValueError, match="at least 28"):
        compute_regime(frame.iloc[:20])


def test_the_reading_becomes_an_event():
    reading = compute_regime(index(calm(300, drift=0.004)))
    event = reading.event(date(2026, 10, 5))
    assert event.label is reading.label and event.session_date == date(2026, 10, 5)
    assert event.event_type == "RegimeComputed"


def test_the_optional_gate():
    gates = {"momentum": frozenset({U, RG})}
    assert regime_allows("momentum", U, gates) and not regime_allows("momentum", D, gates)
    assert not regime_allows("momentum", None, gates)  # unknown regime: a gated strategy waits
    assert regime_allows("mean_reversion", D, gates) and regime_allows("x", None, {})


def test_config_bounds():
    with pytest.raises(ValueError):
        RegimeConfig(vol_high_pct=1.0)
    with pytest.raises(ValueError):
        RegimeConfig(confirm=0)
