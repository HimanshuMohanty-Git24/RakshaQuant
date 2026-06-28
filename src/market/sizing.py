"""
Position Sizing Module

Advanced position sizing calculations for risk management.

Methods:
- Fixed fractional: Risk fixed % of capital per trade
- Kelly Criterion: Optimal sizing based on win rate and payoff
- ATR-based: Position size based on volatility
- Volatility-adjusted: Adjust for current market conditions
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PositionSizeResult:
    """Result of position sizing calculation."""

    shares: int
    position_value: float
    risk_amount: float
    risk_percent: float
    method: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shares": self.shares,
            "position_value": self.position_value,
            "risk_amount": self.risk_amount,
            "risk_percent": self.risk_percent,
            "method": self.method,
            "rationale": self.rationale,
        }


class PositionSizer:
    """
    Advanced position sizing calculator.

    Provides multiple methods for calculating optimal position sizes
    based on risk parameters and market conditions.
    """

    def __init__(
        self,
        capital: float,
        max_position_pct: float = 0.10,  # Max 10% in single position
        max_risk_per_trade: float = 0.02,  # Max 2% risk per trade
        max_total_risk: float = 0.10,  # Max 10% total portfolio risk
    ):
        """
        Initialize position sizer.

        Args:
            capital: Total trading capital
            max_position_pct: Maximum position size as % of capital
            max_risk_per_trade: Maximum risk per trade as % of capital
            max_total_risk: Maximum total portfolio risk
        """
        self.capital = capital
        self.max_position_pct = max_position_pct
        self.max_risk_per_trade = max_risk_per_trade
        self.max_total_risk = max_total_risk

    def fixed_fractional(
        self,
        entry_price: float,
        stop_loss: float,
        risk_percent: float | None = None,
    ) -> PositionSizeResult:
        """
        Fixed fractional position sizing.

        Risk a fixed percentage of capital per trade.

        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            risk_percent: Risk per trade (default: max_risk_per_trade)

        Returns:
            PositionSizeResult with calculated shares
        """
        risk_percent = risk_percent or self.max_risk_per_trade

        # Calculate risk per share
        risk_per_share = abs(entry_price - stop_loss)

        if risk_per_share <= 0:
            logger.warning("Invalid stop loss - risk per share is zero")
            return PositionSizeResult(
                shares=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method="fixed_fractional",
                rationale="Invalid stop loss price",
            )

        # Calculate risk amount
        risk_amount = self.capital * risk_percent

        # Calculate shares
        shares = int(risk_amount / risk_per_share)

        # Apply position size limit
        max_shares = int((self.capital * self.max_position_pct) / entry_price)
        shares = min(shares, max_shares)

        # Ensure at least 1 share if valid
        shares = max(0, shares)

        position_value = shares * entry_price
        actual_risk = shares * risk_per_share
        actual_risk_pct = actual_risk / self.capital if self.capital > 0 else 0

        return PositionSizeResult(
            shares=shares,
            position_value=position_value,
            risk_amount=actual_risk,
            risk_percent=actual_risk_pct,
            method="fixed_fractional",
            rationale=f"Risk {risk_percent:.1%} of capital = ₹{risk_amount:,.0f}. "
            f"Risk per share: ₹{risk_per_share:.2f}",
        )

    def kelly_criterion(
        self,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        win_rate: float,
        fraction: float = 0.5,  # Use half-Kelly for safety
    ) -> PositionSizeResult:
        """
        Kelly Criterion position sizing.

        Calculates optimal position size based on win rate and risk-reward.
        Uses fractional Kelly for more conservative sizing.

        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            target_price: Target price
            win_rate: Historical win rate (0-1)
            fraction: Kelly fraction (0.5 = half-Kelly)

        Returns:
            PositionSizeResult with calculated shares
        """
        # Calculate win/loss amounts
        win_amount = abs(target_price - entry_price)
        loss_amount = abs(entry_price - stop_loss)

        if loss_amount <= 0 or win_amount <= 0:
            return PositionSizeResult(
                shares=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method="kelly_criterion",
                rationale="Invalid price levels",
            )

        # Risk-reward ratio (b in Kelly formula)
        b = win_amount / loss_amount

        # Kelly formula: f* = (bp - q) / b
        # where p = win rate, q = loss rate (1-p), b = win/loss ratio
        p = win_rate
        q = 1 - p

        kelly_fraction = (b * p - q) / b

        # Apply fractional Kelly and caps
        if kelly_fraction <= 0:
            return PositionSizeResult(
                shares=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method="kelly_criterion",
                rationale=f"Negative Kelly ({kelly_fraction:.2%}) - trade has negative expectancy",
            )

        # Apply fraction (half-Kelly) and cap at max risk
        position_pct = kelly_fraction * fraction
        position_pct = min(position_pct, self.max_position_pct)

        # Calculate shares
        position_value = self.capital * position_pct
        shares = int(position_value / entry_price)

        actual_risk = shares * loss_amount
        actual_risk_pct = actual_risk / self.capital if self.capital > 0 else 0

        return PositionSizeResult(
            shares=shares,
            position_value=shares * entry_price,
            risk_amount=actual_risk,
            risk_percent=actual_risk_pct,
            method="kelly_criterion",
            rationale=f"Full Kelly: {kelly_fraction:.1%}, Using: {position_pct:.1%} "
            f"(win rate: {win_rate:.0%}, R:R: {b:.1f}:1)",
        )

    def atr_based(
        self,
        entry_price: float,
        atr: float,
        atr_multiplier: float = 2.0,
        risk_percent: float | None = None,
    ) -> PositionSizeResult:
        """
        ATR-based position sizing.

        Uses Average True Range to set stop loss and calculate position size.
        More volatile stocks get smaller positions.

        Args:
            entry_price: Entry price
            atr: Average True Range value
            atr_multiplier: ATR multiplier for stop (default 2x ATR)
            risk_percent: Risk per trade (default: max_risk_per_trade)

        Returns:
            PositionSizeResult with calculated shares
        """
        risk_percent = risk_percent or self.max_risk_per_trade

        # Calculate stop distance based on ATR
        stop_distance = atr * atr_multiplier

        if stop_distance <= 0:
            return PositionSizeResult(
                shares=0,
                position_value=0,
                risk_amount=0,
                risk_percent=0,
                method="atr_based",
                rationale="Invalid ATR value",
            )

        # Calculate position size
        risk_amount = self.capital * risk_percent
        shares = int(risk_amount / stop_distance)

        # Apply position size limit
        max_shares = int((self.capital * self.max_position_pct) / entry_price)
        shares = min(shares, max_shares)

        position_value = shares * entry_price
        actual_risk = shares * stop_distance
        actual_risk_pct = actual_risk / self.capital if self.capital > 0 else 0

        # Calculate implied stop loss
        stop_loss = entry_price - stop_distance

        return PositionSizeResult(
            shares=shares,
            position_value=position_value,
            risk_amount=actual_risk,
            risk_percent=actual_risk_pct,
            method="atr_based",
            rationale=f"ATR: ₹{atr:.2f}, Stop distance: ₹{stop_distance:.2f} ({atr_multiplier}x ATR). "
            f"Implied stop: ₹{stop_loss:.2f}",
        )

    def volatility_adjusted(
        self,
        entry_price: float,
        stop_loss: float,
        current_volatility: float,
        avg_volatility: float,
        base_risk_percent: float | None = None,
    ) -> PositionSizeResult:
        """
        Volatility-adjusted position sizing.

        Reduces position size in high volatility, increases in low volatility.

        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            current_volatility: Current market volatility
            avg_volatility: Average historical volatility
            base_risk_percent: Base risk per trade

        Returns:
            PositionSizeResult with calculated shares
        """
        base_risk = base_risk_percent or self.max_risk_per_trade

        if avg_volatility <= 0:
            avg_volatility = current_volatility or 1.0

        # Calculate volatility ratio
        vol_ratio = current_volatility / avg_volatility

        # Adjust risk based on volatility
        # High vol (ratio > 1) = reduce risk
        # Low vol (ratio < 1) = increase risk (capped)
        adjusted_risk = base_risk / max(vol_ratio, 0.5)
        adjusted_risk = min(adjusted_risk, base_risk * 1.5)  # Cap at 1.5x
        adjusted_risk = min(adjusted_risk, self.max_risk_per_trade)

        # Use fixed fractional with adjusted risk
        result = self.fixed_fractional(entry_price, stop_loss, adjusted_risk)

        return PositionSizeResult(
            shares=result.shares,
            position_value=result.position_value,
            risk_amount=result.risk_amount,
            risk_percent=result.risk_percent,
            method="volatility_adjusted",
            rationale=f"Vol ratio: {vol_ratio:.2f}. Base risk {base_risk:.1%} adjusted to {adjusted_risk:.1%}. "
            f"{result.rationale}",
        )

    def calculate_optimal(
        self,
        entry_price: float,
        stop_loss: float,
        target_price: float | None = None,
        atr: float | None = None,
        win_rate: float | None = None,
        current_volatility: float | None = None,
        avg_volatility: float | None = None,
    ) -> PositionSizeResult:
        """
        Calculate optimal position size using the best available method.

        Prioritizes methods based on available data:
        1. Kelly Criterion (if win_rate and target available)
        2. Volatility-adjusted (if volatility data available)
        3. ATR-based (if ATR available)
        4. Fixed fractional (fallback)

        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            target_price: Target price (optional)
            atr: Average True Range (optional)
            win_rate: Historical win rate (optional)
            current_volatility: Current volatility (optional)
            avg_volatility: Average volatility (optional)

        Returns:
            PositionSizeResult with optimal sizing
        """
        # Try Kelly Criterion first (most sophisticated)
        if target_price and win_rate and win_rate > 0.3:
            kelly_result = self.kelly_criterion(entry_price, stop_loss, target_price, win_rate)
            if kelly_result.shares > 0:
                return kelly_result

        # Try volatility-adjusted
        if current_volatility and avg_volatility:
            return self.volatility_adjusted(
                entry_price, stop_loss, current_volatility, avg_volatility
            )

        # Try ATR-based
        if atr and atr > 0:
            return self.atr_based(entry_price, atr)

        # Fallback to fixed fractional
        return self.fixed_fractional(entry_price, stop_loss)


# ===========================================
# Helper Functions
# ===========================================


def calculate_position_size(
    capital: float,
    entry_price: float,
    stop_loss: float,
    target_price: float | None = None,
    risk_per_trade: float = 0.02,
    max_position_pct: float = 0.10,
    **kwargs,
) -> PositionSizeResult:
    """
    Convenience function for position sizing.

    Args:
        capital: Trading capital
        entry_price: Entry price
        stop_loss: Stop loss price
        target_price: Target price (optional)
        risk_per_trade: Risk per trade (default 2%)
        max_position_pct: Max position size (default 10%)
        **kwargs: Additional parameters for advanced methods

    Returns:
        PositionSizeResult
    """
    sizer = PositionSizer(
        capital=capital,
        max_position_pct=max_position_pct,
        max_risk_per_trade=risk_per_trade,
    )

    return sizer.calculate_optimal(
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        **kwargs,
    )


def calculate_portfolio_heat(
    positions: list[dict[str, Any]],
    capital: float,
) -> dict[str, float]:
    """
    Calculate current portfolio heat (total risk exposure).

    Args:
        positions: List of open positions with entry_price, stop_loss, quantity
        capital: Total capital

    Returns:
        Dict with heat metrics
    """
    total_risk = 0.0
    position_risks = []

    for pos in positions:
        entry = pos.get("entry_price", 0)
        stop = pos.get("stop_loss", 0)
        qty = pos.get("quantity", 0)

        risk = abs(entry - stop) * qty
        total_risk += risk
        position_risks.append(
            {
                "symbol": pos.get("symbol", "unknown"),
                "risk": risk,
                "risk_pct": risk / capital if capital > 0 else 0,
            }
        )

    return {
        "total_risk": total_risk,
        "total_risk_pct": total_risk / capital if capital > 0 else 0,
        "position_count": len(positions),
        "positions": position_risks,
    }
