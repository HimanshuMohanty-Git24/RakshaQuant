"""Plan M5.4: the deterministic decision engine, end to end through the RiskGate and the OMS."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from src.decision.engine import DecisionConfig, DecisionEngine
from src.domain.types import Quote, Regime, Side, Verdict
from src.features.technical import Features
from src.marketdata.history import DailySeries
from src.strategies.base import Detection, reason
from src.strategies.policy import Proposal, TradePolicy
from tests.oms_harness import INFY, Harness, harness
from tests.test_oms_exit_manager import live_orders, manager
from tests.test_risk_gate import wire

ALL = ("momentum", "mean_reversion", "breakout", "trend_following")


def frame(last_close: float = 1000.0, n: int = 120, seed: int = 4) -> pd.DataFrame:
    """Daily bars ending on Thu 2026-10-01 (the session before the harness's Monday), ~2% ranges."""
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    close = close / close[-1] * last_close
    index = pd.bdate_range(end="2026-10-01", periods=n)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": np.full(n, 3e6), "dividend": 0.0, "split": 0.0, "factor": 1.0},
        index=index,
    )  # fmt: skip


@dataclass
class FakeMarket:
    h: Harness
    series: dict[str, DailySeries] = field(default_factory=dict)
    lagging: set[str] = field(default_factory=set)
    ltp: float = 1001.0
    fail_requote: bool = False
    requoted: list[set[str]] = field(default_factory=list)

    def daily(self, instrument_key: str) -> DailySeries | None:
        return self.series.get(instrument_key)

    def is_lagging(self, instrument_key: str) -> bool:
        return instrument_key in self.lagging

    async def requote(self, instrument_keys: Collection[str]) -> Mapping[str, Quote]:
        self.requoted.append(set(instrument_keys))
        if self.fail_requote:
            raise TimeoutError("feed down")
        self.h.quote(self.ltp)
        assert self.h.last_quote is not None
        return {INFY.key: self.h.last_quote}


@dataclass(frozen=True)
class Always:
    """A stub strategy that always fires (the real ones are tested in test_strategies)."""

    name: str
    side: Side = Side.BUY
    base: float = 0.6
    rsi_contrarian: bool = False

    def detect(self, f: Features) -> Detection | None:
        return Detection(self.side, self.base, (reason("stub", True),))


@dataclass(frozen=True)
class Never:
    name: str
    rsi_contrarian: bool = False

    def detect(self, f: Features) -> Detection | None:
        return None


def build(h: Harness, *strategies: Always, advisor=None, **config):
    wire(h)
    market = FakeMarket(h, series={INFY.key: DailySeries(INFY.key, frame())})
    engine = DecisionEngine(
        config=DecisionConfig(**config), market=market, oms=h.oms,
        exits=manager(h, TradePolicy().config.exit_policy()), policy=TradePolicy(),
        clock=h.clock, sink=h.oms._sink, advisor=advisor,
        strategies={**{n: Never(n) for n in ALL}, **{s.name: s for s in strategies}},
    )  # fmt: skip
    return engine, market


async def test_a_signal_becomes_a_sized_entry_with_full_lineage(tmp_path):
    with harness(tmp_path, gate=None) as h:
        engine, market = build(h, Always("momentum"), Always("mean_reversion", Side.SELL),
                               Always("breakout"), Always("trend_following"))  # fmt: skip
        await h.oms.start()
        result = await engine.run_cycle([INFY])

        assert [(s.strategy, s.is_shadow) for s in result.signals] == [
            ("momentum", False), ("mean_reversion", False),
            ("breakout", True), ("trend_following", True),
        ]  # fmt: skip
        assert result.skipped == {f"mean_reversion:{INFY.key}:2026-10-01": "long_only"}
        [(proposal, submitted)] = result.submitted
        assert submitted.status == "SUBMITTED" and submitted.order is not None
        assert submitted.order.quantity == 99  # 10% of 10L at the 1,001 arrival price
        assert market.requoted == [{INFY.key}]  # one re-quote, right before submitting

        intent = proposal.intent
        assert intent.decision_price == Decimal("1000.0")  # the settled close the signal saw
        types = [e.type for e in h.store.read(types=["SignalGenerated", "OrderIntentProposed",
                                                      "RiskDecision", "OrderSubmitted"])]  # fmt: skip
        assert types == ["SignalGenerated"] * 4 + ["OrderIntentProposed", "RiskDecision",
                                                   "OrderSubmitted"]  # fmt: skip
        (decision,) = h.events("RiskDecision")
        assert decision.ref_price == Decimal("1001.0")  # the arrival price
        assert decision.decision_id == intent.decision_id == proposal.signal.decision_id

        h.quote(1002.0)  # fills; the exit manager re-anchors the stop to the fill
        await engine._exits.on_quote(h.last_quote)  # type: ignore[arg-type]
        assert [i.split(":")[-1] for i in live_orders(h)] == ["stop-2026-10-05-v1"]


async def test_higher_agreement_goes_first(tmp_path):
    with harness(tmp_path, gate=None) as h:
        engine, _ = build(h, Always("momentum", base=0.2), Always("mean_reversion", base=0.9))
        await h.oms.start()
        result = await engine.run_cycle([INFY])
        assert [(p.intent.strategy, r.status) for p, r in result.submitted] == [
            ("mean_reversion", "SUBMITTED"), ("momentum", "BLOCKED"),  # PF_DUPLICATE
        ]  # fmt: skip


async def test_lagging_or_missing_history_is_never_traded(tmp_path):
    with harness(tmp_path, gate=None) as h:
        engine, market = build(h, Always("momentum"))
        market.lagging.add(INFY.key)
        tcs = INFY.model_copy(update={"key": "NSE:EQ:TCS", "symbol": "TCS"})
        result = await engine.run_cycle([INFY, tcs])
        assert result.skipped == {INFY.key: "lagging", "NSE:EQ:TCS": "no_history"}
        assert result.signals == [] and market.requoted == []


async def test_a_broken_series_is_isolated(tmp_path):
    with harness(tmp_path, gate=None) as h:
        engine, market = build(h, Always("momentum"))
        market.series[INFY.key] = DailySeries(INFY.key, frame().drop(columns="volume"))
        result = await engine.run_cycle([INFY])
        assert result.skipped[INFY.key].startswith("error")
        assert [a.key for a in h.events("Alert")] == ["decision_symbol_failed"]


async def test_the_optional_regime_gate(tmp_path):
    with harness(tmp_path, gate=None) as h:
        gates = {"momentum": frozenset({Regime.TRENDING_UP})}
        engine, _ = build(h, Always("momentum"), regime_gates=gates)
        result = await engine.run_cycle([INFY], regime=Regime.RANGING)
        assert list(result.skipped.values()) == ["regime ranging"] and result.submitted == []


async def test_an_advisor_can_only_veto(tmp_path):
    class Veto:
        async def review(self, proposal: Proposal, features: Features) -> Verdict:
            return Verdict.VETO

    class Broken:
        async def review(self, proposal: Proposal, features: Features) -> Verdict:
            raise RuntimeError("provider down")

    with harness(tmp_path, gate=None) as h:
        engine, _ = build(h, Always("momentum"), advisor=Veto())
        await h.oms.start()
        result = await engine.run_cycle([INFY])
        assert len(result.vetoed) == 1 and result.submitted == [] and h.oms.orders == {}

    with harness(tmp_path / "b", gate=None) as h:
        engine, _ = build(h, Always("momentum"), advisor=Broken())
        await h.oms.start()
        result = await engine.run_cycle([INFY])  # a failing advisor = ABSTAIN: the decision stands
        assert [r.status for _, r in result.submitted] == ["SUBMITTED"]


async def test_a_failed_requote_cannot_trade_on_stale_prices(tmp_path):
    with harness(tmp_path, gate=None) as h:
        engine, market = build(h, Always("momentum"))
        market.fail_requote = True
        result = await engine.run_cycle([INFY])
        assert [a.key for a in h.events("Alert")] == ["decision_requote_failed"]
        [(_, submitted)] = result.submitted
        assert submitted.status == "BLOCKED" and "SYS_DATA_STALE" in submitted.message


def test_the_fixture_series_is_settled_before_the_session():
    assert DailySeries(INFY.key, frame()).last_date == date(2026, 10, 1)
