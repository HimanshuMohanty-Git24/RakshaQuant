"""
Bounded risk limits (plan M4.5; audit F-15).

Every limit the risk engine enforces lives here, with explicit bounds; nothing is hardcoded in
the checks. Values come from ``RISK_*`` environment variables (or ``.env``), else the defaults.
An out-of-bounds value **raises** at startup (``ConfigError``, exit code 2) instead of degrading
silently. Every ``RiskDecision`` records :meth:`RiskLimits.limits_hash`, so a decision can
always be tied to the exact limits it was made under.

Defaults absorb the legacy hardcoded limits (5 positions, 50% exposure, 5% drawdown, R:R 1.5,
5% max stop, 30% sector) and the month-1 design (CNC long-only, momentum + mean reversion).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.errors import ConfigError
from src.config.settings import ENV_FILE

KillAction = Literal["HALT_NEW", "FLATTEN"]


class RiskLimits(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RISK_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- order sizing (RESIZE) -------------------------------------------------------------
    risk_per_trade: float = Field(
        default=0.02, gt=0, le=0.05, description="Equity at risk per trade"
    )
    max_position_pct: float = Field(
        default=0.10, gt=0, le=0.25, description="Notional cap vs equity"
    )
    max_position_inr: float = Field(default=100_000.0, gt=0, description="Notional cap in INR")
    adv_pct: float = Field(default=0.01, gt=0, le=0.10, description="Order size cap vs 20-day ADV")
    kelly_enabled: bool = Field(
        default=False, description="Kelly sizing (only with enough real trades)"
    )
    kelly_min_trades: int = Field(
        default=30, ge=30, description="Real net trades before Kelly applies"
    )
    kelly_scale: float = Field(default=0.25, gt=0, le=1.0, description="Fraction of full Kelly")

    # -- stops and prices -----------------------------------------------------------------------
    min_rr: float = Field(
        default=1.5, ge=0.5, le=10.0, description="Minimum reward:risk from the price"
    )
    stop_atr_min: float = Field(default=0.5, gt=0, le=10.0, description="Tightest stop, in ATRs")
    stop_atr_max: float = Field(default=3.0, gt=0, le=10.0, description="Widest stop, in ATRs")
    max_stop_pct: float = Field(default=0.05, gt=0, le=0.25, description="Widest stop vs price")
    price_collar_pct: float = Field(
        default=0.03, gt=0, le=0.20, description="Decision vs fresh price"
    )
    allow_short: bool = Field(default=False, description="Opening shorts (never for CNC)")

    # -- portfolio --------------------------------------------------------------------------------
    max_positions: int = Field(default=5, ge=1, le=20)
    max_gross_exposure_pct: float = Field(default=0.50, gt=0, le=1.0)
    max_sector_pct: float = Field(default=0.30, gt=0, le=1.0)
    max_heat_pct: float = Field(
        default=0.06, gt=0, le=0.25, description="Sum of open risk vs equity"
    )
    daily_loss_limit_pct: float = Field(
        default=0.01, gt=0, le=0.10, description="MTM vs start of day"
    )
    max_drawdown_pct: float = Field(
        default=0.05, gt=0, le=0.25, description="Equity vs running peak"
    )
    max_entries_per_day: int = Field(default=10, ge=1, le=100)

    # -- strategies -------------------------------------------------------------------------------
    enabled_strategies: tuple[str, ...] = Field(
        default=("momentum", "mean_reversion"),
        description="Strategies allowed to trade; anything else is shadow-only (plan D8)",
    )
    strategy_capital_pct: float = Field(
        default=0.50, gt=0, le=1.0, description="Deployed per strategy"
    )
    strategy_daily_loss_pct: float = Field(default=0.005, gt=0, le=0.10)
    strategy_max_consec_losses: int = Field(default=4, ge=1, le=50)
    strategy_orders_per_min: int = Field(default=5, ge=1, le=60)

    # -- system -------------------------------------------------------------------------------------
    max_quote_age_s: float = Field(
        default=1200.0, gt=0, le=3600.0, description="Per audit F.0 (YF)"
    )
    max_open_orders: int = Field(default=20, ge=1, le=200)
    orders_per_min: int = Field(
        default=10, ge=1, le=120, description="Far below SEBI/broker limits"
    )
    reject_storm_count: int = Field(default=5, ge=1, le=100)
    reject_storm_window_s: float = Field(default=600.0, gt=0, le=86_400.0)

    # -- kill switches -------------------------------------------------------------------------------
    daily_loss_action: KillAction = "FLATTEN"
    drawdown_action: KillAction = "FLATTEN"
    rearm_on_new_day: bool = Field(
        default=False, description="Re-arm a daily-loss trip next IST day"
    )
    flatten_escalate_after: int = Field(
        default=3, ge=1, le=20, description="Attempts before CRITICAL"
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.stop_atr_min >= self.stop_atr_max:
            raise ValueError("stop_atr_min must be below stop_atr_max")
        if self.risk_per_trade > self.max_position_pct:
            raise ValueError("risk_per_trade cannot exceed max_position_pct")
        if self.max_position_pct > self.max_gross_exposure_pct:
            raise ValueError("max_position_pct cannot exceed max_gross_exposure_pct")
        if not self.enabled_strategies:
            raise ValueError("enabled_strategies is empty: nothing could ever trade")
        return self

    def limits_hash(self) -> str:
        """A short, stable fingerprint of every limit (recorded on each RiskDecision)."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def load_risk_limits(values: Mapping[str, object] | None = None) -> RiskLimits:
    """Validated limits: from ``RISK_*`` env/.env, or exactly ``values`` when given.

    Any violation is a configuration error (``ConfigError``, exit code 2).
    """
    try:
        return RiskLimits() if values is None else RiskLimits.model_validate(dict(values))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'limits'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid risk limits: {problems}") from None
