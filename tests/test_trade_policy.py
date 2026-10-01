"""Plan M5.2: TradePolicy - long-only CNC proposals, sizing left to risk, exits from the fill."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from src.config.limits import load_risk_limits
from src.domain.clock import ReplayClock
from src.domain.sink import RecordingSink
from src.domain.types import (
    Instrument,
    IntentKind,
    MarketDataSource,
    Product,
    Quote,
    RiskOutcome,
    Side,
    Signal,
)
from src.oms.exit_manager import ExitPolicy
from src.risk.engine import RiskEngine
from src.risk.snapshot import MarketFacts, RiskSnapshot
from src.strategies.policy import Proposal, Skipped, TradePolicy, TradePolicyConfig

NOW = datetime(2026, 10, 5, 3, 55, tzinfo=UTC)
INFY = Instrument.nse_equity("INFY", sector="IT")


def signal(side: Side = Side.BUY, *, shadow: bool = False, strategy: str = "momentum") -> Signal:
    return Signal(
        signal_id=f"{strategy}:{INFY.key}:2026-10-01", decision_id="d1", instrument_key=INFY.key,
        strategy=strategy, side=side, bar_date=date(2026, 10, 1), agreement_score=0.6,
        stop_atr_mult=2.0, target_atr_mult=3.0, is_shadow=shadow, generated_at=NOW,
    )  # fmt: skip


def propose(sig: Signal, **kw: object) -> Proposal | Skipped:
    args: dict[str, object] = {"book_id": "A", "price": Decimal(1000), "atr": Decimal(20),
                               "decision_ts": NOW}  # fmt: skip
    args.update(kw)
    return TradePolicy().propose(sig, INFY, **args)  # type: ignore[arg-type]


def test_a_buy_signal_becomes_an_unsized_long_cnc_entry():
    sink = RecordingSink(ReplayClock(NOW))
    out = propose(signal(), sink=sink)
    assert isinstance(out, Proposal)
    i = out.intent
    assert i.intent_id == f"A:momentum:{INFY.key}:2026-10-01:entry"
    assert (i.side, i.kind, i.product, i.quantity, i.reduce_only) == (
        Side.BUY, IntentKind.OPEN, Product.CNC, None, False,
    )  # fmt: skip
    assert (i.stop_price, i.target_price) == (Decimal(960), Decimal(1060))
    assert i.signal_id == out.signal.signal_id and i.decision_id == "d1" and out.atr == 20
    assert [e.type for e in sink.events] == ["OrderIntentProposed"]


@pytest.mark.parametrize(
    ("sig", "kw", "why"),
    [
        (signal(shadow=True), {}, "shadow"),
        (signal(Side.SELL), {}, "long_only"),
        (signal(), {"atr": None}, "no_atr"),
        (signal(), {"atr": Decimal(600)}, "no_atr"),  # a stop at or below zero
    ],
)
def test_what_never_becomes_an_order(sig, kw, why):
    out = propose(sig, **kw)
    assert isinstance(out, Skipped) and out.reason == why


def test_the_same_signal_in_another_book_is_another_order():
    a, b = propose(signal()), propose(signal(), book_id="B")
    assert isinstance(a, Proposal) and isinstance(b, Proposal)
    assert a.intent.intent_id != b.intent.intent_id


def test_the_exit_policy_shares_the_multipliers():
    config = TradePolicyConfig(k_stop_atr=1.5, k_target_atr=4.0, max_hold_days=7, partial_at_r=1.0)
    assert config.exit_policy() == ExitPolicy(k_stop_atr=1.5, k_target_atr=4.0, k_trail_atr=2.0,
                                              max_hold_days=7, partial_at_r=1.0)  # fmt: skip
    with pytest.raises(ValueError):
        TradePolicyConfig(max_hold_days=0)
    with pytest.raises(ValueError, match="CNC"):
        TradePolicyConfig(product=Product.MIS)


def test_default_proposals_pass_the_risk_engine_stop_and_rr_checks():
    """2 ATR stop / 3 ATR target is exactly the 1.5 minimum R:R - it must not be rejected."""
    out = propose(signal())
    assert isinstance(out, Proposal)
    quote = Quote(instrument_key=INFY.key, ltp=1000.0, prev_close=995.0, volume_cum=1,
                  exchange_ts=NOW, receipt_ts=NOW, source=MarketDataSource.YFINANCE)  # fmt: skip
    snap = RiskSnapshot(
        now=NOW, environment="paper", equity=Decimal(1_000_000), cash=Decimal(1_000_000),
        sod_equity=Decimal(1_000_000), peak_equity=Decimal(1_000_000),
        market={INFY.key: MarketFacts(quote, Decimal(20), 5e6, MarketDataSource.YFINANCE)},
    )  # fmt: skip
    decision = RiskEngine(load_risk_limits()).evaluate(out.intent, snap).decision
    assert decision.outcome is RiskOutcome.APPROVED and decision.qty_approved == 100
