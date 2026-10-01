"""
LLM pricing (plan M6): USD per 1M tokens per ``provider:model`` from
``src/config/llm_pricing.yaml``, and conversion to INR at the ``USD_INR`` setting.

* The longest matching prefix wins (``anthropic:claude-haiku-4-5`` prices the dated id too).
* Models ending in ``:free`` cost 0.
* A provider-reported cost (OpenRouter's ``usage.cost``) overrides the table.
* An unknown model costs ``None`` - recorded as unknown and flagged, never guessed as 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from src.llm.registry import ModelSpec
from src.llm.types import Usage

DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[1] / "config" / "llm_pricing.yaml"
MILLION = Decimal(1_000_000)
ZERO = Decimal(0)


@dataclass(frozen=True)
class Price:
    input_per_m: Decimal
    output_per_m: Decimal
    cache_read: Decimal = Decimal(1)  # multiple of the input price
    cache_write: Decimal = Decimal(1)


class PricingTable:
    def __init__(self, prices: dict[str, Price]) -> None:
        self._prices = dict(prices)

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_PRICING_PATH) -> PricingTable:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        providers: dict[str, Any] = data.get("providers") or {}
        prices: dict[str, Price] = {}
        for key, entry in (data.get("models") or {}).items():
            provider = str(key).split(":", 1)[0]
            extra = providers.get(provider) or {}
            prices[str(key)] = Price(
                input_per_m=_dec(entry["input"]),
                output_per_m=_dec(entry["output"]),
                cache_read=_dec(extra.get("cache_read", 1)),
                cache_write=_dec(extra.get("cache_write", 1)),
            )
        return cls(prices)

    def price(self, model: ModelSpec) -> Price | None:
        if model.is_free:
            return Price(ZERO, ZERO)
        spec = model.spec
        matches = [k for k in self._prices if spec.startswith(k)]
        return self._prices[max(matches, key=len)] if matches else None

    def cost_usd(self, model: ModelSpec, usage: Usage) -> Decimal | None:
        if usage.reported_cost_usd is not None:
            return usage.reported_cost_usd
        price = self.price(model)
        if price is None:
            return None
        total = (
            usage.input_tokens * price.input_per_m
            + usage.output_tokens * price.output_per_m
            + usage.cache_read_tokens * price.input_per_m * price.cache_read
            + usage.cache_write_tokens * price.input_per_m * price.cache_write
        ) / MILLION
        return total.quantize(Decimal("0.000001"))


def to_inr(usd: Decimal | None, usd_inr: float) -> Decimal | None:
    if usd is None:
        return None
    return (usd * Decimal(str(usd_inr))).quantize(Decimal("0.0001"))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))
