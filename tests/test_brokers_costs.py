"""Plan M3.2: product-aware NSE charges (Dhan rates verified 2026-10-02), golden values."""

from decimal import Decimal

import pytest

from src.brokers.simulated.costs import NSECostSchedule
from src.domain.types import Product, Side

COSTS = NSECostSchedule.from_yaml()
LAKH = Decimal(100_000)


def leg(product: Product, side: Side, notional: Decimal = LAKH, dp: bool = False) -> Decimal:
    return COSTS.charges(product=product, side=side, notional=notional, dp_applies=dp).total


def test_cnc_buy_leg_breakdown():
    c = COSTS.charges(product=Product.CNC, side=Side.BUY, notional=LAKH)
    # STT 0.1% = 100; NSE 0.0030699% = 3.07; SEBI 0.0001% = 0.10; stamp 0.015% = 15;
    # GST 18% x (0 + 3.0699 + 0.10 + 0.0001) = 0.57.
    assert (c.brokerage, c.stt, c.exchange, c.sebi, c.stamp, c.dp, c.gst) == tuple(
        Decimal(x) for x in ("0.00", "100.00", "3.07", "0.10", "15.00", "0.00", "0.57")
    )
    assert c.total == Decimal("118.74")


def test_golden_cnc_round_trip_at_one_lakh():
    without_dp = leg(Product.CNC, Side.BUY) + leg(Product.CNC, Side.SELL)
    assert without_dp == Decimal("222.48")
    with_dp = leg(Product.CNC, Side.BUY) + leg(Product.CNC, Side.SELL, dp=True)
    # DP Rs 12.50 + 18% GST = 14.75. The audit's reference "~Rs 238" already includes DP.
    assert with_dp == Decimal("237.23")
    assert abs(with_dp - 238) / 238 <= Decimal("0.02")


def test_golden_mis_round_trip_at_one_lakh():
    # Brokerage min(20, 0.03%) = 20 per order; STT 0.025% on the sell; stamp 0.003% on the buy.
    rt = leg(Product.MIS, Side.BUY) + leg(Product.MIS, Side.SELL)
    assert rt == Decimal("82.68")
    assert abs(rt - 82) / 82 <= Decimal("0.02")


def test_mis_brokerage_is_capped_and_proportional_below_the_cap():
    big = COSTS.charges(product=Product.MIS, side=Side.BUY, notional=LAKH)
    small = COSTS.charges(product=Product.MIS, side=Side.BUY, notional=Decimal(10_000))
    assert big.brokerage == Decimal("20.00") and small.brokerage == Decimal("3.00")


def test_dp_only_on_the_days_first_sell_of_an_isin_and_never_on_buys():
    assert COSTS.charges(product=Product.CNC, side=Side.SELL, notional=LAKH).dp == 0
    assert COSTS.charges(
        product=Product.CNC, side=Side.SELL, notional=LAKH, dp_applies=True
    ).dp == Decimal("12.50")
    assert COSTS.charges(product=Product.CNC, side=Side.BUY, notional=LAKH, dp_applies=True).dp == 0
    assert (
        COSTS.charges(product=Product.MIS, side=Side.SELL, notional=LAKH, dp_applies=True).dp == 0
    )


def test_zero_schedule_and_zero_notional():
    assert (
        NSECostSchedule.zero()
        .charges(product=Product.CNC, side=Side.SELL, notional=LAKH, dp_applies=True)
        .total
        == 0
    )
    assert COSTS.charges(product=Product.CNC, side=Side.BUY, notional=Decimal(0)).total == 0


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError, match="notional"):
        COSTS.charges(product=Product.CNC, side=Side.BUY, notional=Decimal(-1))
    with pytest.raises(ValueError, match="no cost schedule"):
        COSTS.charges(product=Product.NRML, side=Side.BUY, notional=LAKH)
    bad = {
        "schema_version": 1,
        "exchange_txn_pct": -1,
        "sebi_pct": 0,
        "ipft_pct": 0,
        "gst_pct": 18,
        "products": {},
    }
    with pytest.raises(ValueError, match="exchange_txn_pct"):
        NSECostSchedule.from_mapping(bad)
    with pytest.raises(ValueError, match="schema"):
        NSECostSchedule.from_mapping({**bad, "schema_version": 2})


def test_schedule_records_its_source():
    assert COSTS.source == "https://dhan.co/pricing/"
