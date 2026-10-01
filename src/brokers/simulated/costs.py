"""
Product-aware NSE charges (plan M3.2; audit §M.3), rates in ``src/config/costs_nse.yaml``.

Each component is computed exactly in ``Decimal`` and rounded to the paisa. DP charges apply
once per ISIN per sell day (the caller says whether this sell is the day's first for the ISIN).
Slippage, spread and impact are *not* charges: they belong to the fill model.

``NSECostSchedule.zero()`` gives ideal, cost-free fills for pure-mechanics tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from src.domain.types import Product, Side

DEFAULT_COSTS_PATH = Path(__file__).resolve().parents[2] / "config" / "costs_nse.yaml"
_PAISA = Decimal("0.01")
_HUNDRED = Decimal(100)


def _paise(value: Decimal) -> Decimal:
    return value.quantize(_PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ProductRates:
    brokerage_pct: Decimal
    brokerage_cap_inr: Decimal  # 0 = uncapped
    stt_buy_pct: Decimal
    stt_sell_pct: Decimal
    stamp_buy_pct: Decimal
    dp_per_sell_inr: Decimal


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange: Decimal
    sebi: Decimal
    ipft: Decimal
    stamp: Decimal
    dp: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.exchange
            + self.sebi
            + self.ipft
            + self.stamp
            + self.dp
            + self.gst
        )

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange": self.exchange,
            "sebi": self.sebi,
            "ipft": self.ipft,
            "stamp": self.stamp,
            "dp": self.dp,
            "gst": self.gst,
        }


_ZERO_CHARGES = ChargeBreakdown(*(Decimal(0),) * 8)


@dataclass(frozen=True)
class NSECostSchedule:
    exchange_txn_pct: Decimal
    sebi_pct: Decimal
    ipft_pct: Decimal
    gst_pct: Decimal
    products: dict[Product, ProductRates]
    source: str = ""

    @classmethod
    def zero(cls) -> NSECostSchedule:
        """No charges at all (ideal fills for mechanics tests)."""
        nil = ProductRates(*(Decimal(0),) * 6)
        return cls(Decimal(0), Decimal(0), Decimal(0), Decimal(0), dict.fromkeys(Product, nil))

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_COSTS_PATH) -> NSECostSchedule:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> NSECostSchedule:
        if data.get("schema_version") != 1:
            raise ValueError(f"unsupported cost schema {data.get('schema_version')!r}")

        def dec(value: Any, name: str) -> Decimal:
            number = Decimal(str(value))
            if number < 0 or not number.is_finite():
                raise ValueError(f"cost rate {name} must be a finite number >= 0, got {value!r}")
            return number

        products = {
            Product(name): ProductRates(
                brokerage_pct=dec(spec["brokerage_pct"], f"{name}.brokerage_pct"),
                brokerage_cap_inr=dec(spec["brokerage_cap_inr"], f"{name}.brokerage_cap_inr"),
                stt_buy_pct=dec(spec["stt_buy_pct"], f"{name}.stt_buy_pct"),
                stt_sell_pct=dec(spec["stt_sell_pct"], f"{name}.stt_sell_pct"),
                stamp_buy_pct=dec(spec["stamp_buy_pct"], f"{name}.stamp_buy_pct"),
                dp_per_sell_inr=dec(spec["dp_per_sell_inr"], f"{name}.dp_per_sell_inr"),
            )
            for name, spec in data["products"].items()
        }
        return cls(
            exchange_txn_pct=dec(data["exchange_txn_pct"], "exchange_txn_pct"),
            sebi_pct=dec(data["sebi_pct"], "sebi_pct"),
            ipft_pct=dec(data["ipft_pct"], "ipft_pct"),
            gst_pct=dec(data["gst_pct"], "gst_pct"),
            products=products,
            source=str(data.get("source", "")),
        )

    def charges(
        self, *, product: Product, side: Side, notional: Decimal, dp_applies: bool = False
    ) -> ChargeBreakdown:
        """Charges for one executed order leg of ``notional`` rupees."""
        if notional < 0:
            raise ValueError("notional must be >= 0")
        if notional == 0:
            return _ZERO_CHARGES
        try:
            rates = self.products[product]
        except KeyError:
            raise ValueError(f"no cost schedule for product {product}") from None

        def pct(rate: Decimal) -> Decimal:
            return notional * rate / _HUNDRED

        brokerage = pct(rates.brokerage_pct)
        if rates.brokerage_cap_inr > 0:
            brokerage = min(brokerage, rates.brokerage_cap_inr)
        buy = side is Side.BUY
        stt = pct(rates.stt_buy_pct if buy else rates.stt_sell_pct)
        stamp = pct(rates.stamp_buy_pct) if buy else Decimal(0)
        exchange, sebi, ipft = pct(self.exchange_txn_pct), pct(self.sebi_pct), pct(self.ipft_pct)
        dp = rates.dp_per_sell_inr if (not buy and dp_applies) else Decimal(0)
        gst = (brokerage + exchange + sebi + ipft + dp) * self.gst_pct / _HUNDRED
        return ChargeBreakdown(
            brokerage=_paise(brokerage),
            stt=_paise(stt),
            exchange=_paise(exchange),
            sebi=_paise(sebi),
            ipft=_paise(ipft),
            stamp=_paise(stamp),
            dp=_paise(dp),
            gst=_paise(gst),
        )
