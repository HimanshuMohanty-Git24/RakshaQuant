"""Plan M5.1: features on settled bars and one module per strategy, ported faithfully."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from src.domain.types import Side
from src.features.technical import Features, compute_features
from src.market.indicators import Timeframe, calculate_indicators
from src.market.signals import SignalEngine, SignalType, StrategyType
from src.strategies import generate_signals, registry
from src.strategies.base import agreement
from src.strategies.mean_reversion import MeanReversion
from src.strategies.momentum import Momentum

KEY = "NSE:EQ:INFY"
NOW = datetime(2026, 10, 5, 3, 50, tzinfo=UTC)


def walk(n: int = 260, seed: int = 7, drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    index = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    )


def feats(**kw: object) -> Features:
    base: dict[str, object] = {
        "instrument_key": KEY, "bar_date": date(2026, 10, 1), "bars": 250, "open": 1000.0,
        "high": 1010.0, "low": 990.0, "close": 1000.0, "volume": 2e6, "atr_14": 20.0,
    }  # fmt: skip
    base.update(kw)
    return Features(**base)  # type: ignore[arg-type]


def test_features_match_the_legacy_indicators():
    frame = walk()
    new = compute_features(frame, KEY)
    old = calculate_indicators(frame, "INFY", Timeframe.D1)
    pairs = [
        (new.rsi_14, old.rsi), (new.macd, old.macd), (new.macd_signal, old.macd_signal),
        (new.macd_hist, old.macd_histogram), (new.adx_14, old.adx), (new.plus_di_14, old.plus_di),
        (new.minus_di_14, old.minus_di), (new.atr_14, old.atr), (new.bb_upper, old.bb_upper),
        (new.bb_middle, old.bb_middle), (new.bb_lower, old.bb_lower),
        (new.bb_percent, old.bb_percent), (new.ema[21], old.ema[21]), (new.sma[200], old.sma[200]),
    ]  # fmt: skip
    for got, want in pairs:
        assert got == pytest.approx(want, rel=1e-9)
    assert new.bar_date == frame.index[-1].date() and new.bars == 260
    assert new.prev_close == pytest.approx(frame["close"].iloc[-2])
    assert new.adv20_shares == pytest.approx(frame["volume"].iloc[-20:].mean())
    assert new.sigma_daily is not None and 0.01 < new.sigma_daily < 0.02


def test_warm_up_values_are_none_never_nan():
    f = compute_features(walk(12), KEY)
    assert f.rsi_14 is None and f.macd is None and f.adx_14 is None and f.atr_14 is None
    assert f.bb_upper is None and 21 not in f.ema and f.adv20_shares is None
    flat = f.as_dict()
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in flat.values())
    with pytest.raises(ValueError, match="missing columns"):
        compute_features(walk(30).drop(columns="volume"), KEY)


def test_every_strategy_detects_what_the_legacy_engine_did():
    """Rolling over three random walks: the same side on every bar, for every strategy."""
    legacy = SignalEngine()
    ported = registry()
    fired = dict.fromkeys(StrategyType, 0)
    for seed, drift in ((1, 0.0), (2, 0.002), (3, -0.002)):
        frame = walk(320, seed, drift)
        for end in range(60, 321, 4):
            window = frame.iloc[:end]
            old_ind = calculate_indicators(window, "X", Timeframe.D1)
            new_f = compute_features(window, KEY)
            for strategy in StrategyType:
                old = legacy._run_strategy(old_ind, strategy)
                got = ported[strategy.value].detect(new_f)
                want = (
                    None if old is None or old.signal_type is SignalType.HOLD else old.signal_type
                )
                assert (got.side.value if got else None) == (want.value if want else None)
                fired[strategy] += want is not None
    assert all(fired.values()), fired  # every strategy produced real signals to compare


def test_rsi_votes_contrarian_only_for_mean_reversion():
    f = feats(rsi_14=25.0, macd_hist=None, plus_di_14=None, minus_di_14=None)
    assert agreement(f, Side.BUY, rsi_contrarian=True) == 1.0  # low RSI supports reverting up
    assert agreement(f, Side.BUY, rsi_contrarian=False) == 0.0  # but not a trend/momentum buy
    hot = feats(rsi_14=65.0, macd_hist=None)
    assert agreement(hot, Side.BUY, rsi_contrarian=False) == 1.0
    assert agreement(feats(), Side.BUY, rsi_contrarian=False) == 0.5  # nothing to vote


def test_signals_carry_lineage_and_shadow_flags():
    f = feats(rsi_14=25.0, macd_hist=1.5, bb_lower=1001.0, bb_upper=1200.0, bb_middle=1100.0)
    signals = generate_signals(
        f, enabled=["momentum", "mean_reversion"], shadow=["breakout", "momentum"],
        decision_id="d1", generated_at=NOW, stop_atr_mult=2.0, target_atr_mult=3.0,
    )  # fmt: skip
    assert [(s.strategy, s.is_shadow) for s in signals] == [
        ("momentum", False),
        ("mean_reversion", False),
    ]  # momentum stays enabled; breakout found nothing (no squeeze)
    mom = signals[0]
    assert mom.signal_id == f"momentum:{KEY}:2026-10-01" and mom.side is Side.BUY
    assert mom.bar_date == date(2026, 10, 1) and mom.decision_id == "d1"
    names = [r.name for r in mom.reasons]
    assert names[:2] == ["rsi_14", "macd_hist"] and "atr_14" in names
    assert 0 < mom.agreement_score <= 0.95
    with pytest.raises(ValueError, match="unknown"):
        generate_signals(f, enabled=["nope"], shadow=[], decision_id="d", generated_at=NOW,
                         stop_atr_mult=2, target_atr_mult=3)  # fmt: skip


def test_strength_ladders():
    assert Momentum().detect(feats(rsi_14=25.0, macd_hist=1.0)).base_score == 0.8  # type: ignore[union-attr]
    assert Momentum().detect(feats(rsi_14=45.0, macd_hist=1.0)).base_score == 0.4  # type: ignore[union-attr]
    assert Momentum().detect(feats(rsi_14=55.0, macd_hist=1.0)) is None
    band = {"bb_lower": 1000.0, "bb_upper": 1100.0, "bb_middle": 1050.0}
    assert MeanReversion().detect(feats(rsi_14=25.0, **band)).base_score == 0.75  # type: ignore[union-attr]
    assert MeanReversion().detect(feats(rsi_14=45.0, **band)).base_score == 0.5  # type: ignore[union-attr]
    assert MeanReversion().detect(replace(feats(rsi_14=45.0, **band), close=1050.0)) is None
