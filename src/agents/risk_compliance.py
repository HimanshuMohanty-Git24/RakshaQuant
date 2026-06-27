"""
Risk & Compliance Agent Module

Hard gatekeeper that enforces risk rules and compliance constraints.
No LLM required - uses deterministic rules for safety.

Features:
- Position sizing limits
- Sector exposure limits
- Portfolio correlation checks
- Drawdown and daily loss limits
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from src.config import get_settings
from src.utils.market_time import now_ist
from .state import TradingState

logger = logging.getLogger(__name__)


# Sector mappings for NSE stocks (can be extended)
STOCK_SECTORS = {
    # Banking
    "HDFCBANK": "Banking",
    "ICICIBANK": "Banking",
    "SBIN": "Banking",
    "KOTAKBANK": "Banking",
    "AXISBANK": "Banking",
    # IT
    "TCS": "IT",
    "INFY": "IT",
    "WIPRO": "IT",
    "HCLTECH": "IT",
    "TECHM": "IT",
    # Pharma
    "SUNPHARMA": "Pharma",
    "DRREDDY": "Pharma",
    "CIPLA": "Pharma",
    "DIVISLAB": "Pharma",
    # Auto
    "TATAMOTORS": "Auto",
    "MARUTI": "Auto",
    "M&M": "Auto",
    "BAJAJ-AUTO": "Auto",
    # Energy
    "RELIANCE": "Energy",
    "ONGC": "Energy",
    "BPCL": "Energy",
    "IOC": "Energy",
    # Metals
    "TATASTEEL": "Metals",
    "HINDALCO": "Metals",
    "JSWSTEEL": "Metals",
    "COALINDIA": "Metals",
    # FMCG
    "HINDUNILVR": "FMCG",
    "ITC": "FMCG",
    "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG",
    # Telecom
    "BHARTIARTL": "Telecom",
    # Infrastructure
    "ADANIENT": "Infrastructure",
    "LT": "Infrastructure",
    # Financial Services
    "BAJFINANCE": "Financial Services",
    "BAJAJFINSV": "Financial Services",
    "HDFC": "Financial Services",
}


def get_stock_sector(symbol: str) -> str:
    """Get sector for a stock symbol."""
    # Normalize symbol (remove exchange suffix)
    clean_symbol = symbol.replace(".NS", "").upper()
    return STOCK_SECTORS.get(clean_symbol, "Unknown")


@dataclass
class RiskLimits:
    """Risk management limits."""
    
    # Position limits
    max_position_size_pct: float = 10.0  # Max % of capital per position
    max_total_exposure_pct: float = 50.0  # Max total exposure
    max_positions: int = 5  # Max concurrent positions
    
    # Daily limits
    max_daily_trades: int = 50
    max_daily_loss: float = 10000.0  # INR
    
    # Per-trade limits
    min_risk_reward: float = 1.5
    max_stop_loss_pct: float = 5.0
    
    # Drawdown limits
    max_drawdown_pct: float = 5.0
    
    # Time-based limits
    no_trading_before: str = "09:15"  # Market open
    no_trading_after: str = "15:15"   # Before close
    
    # Sector exposure limits
    max_sector_exposure_pct: float = 30.0  # Max % of portfolio per sector
    max_correlated_positions: int = 3  # Max positions in same sector
    
    @classmethod
    def from_settings(cls) -> "RiskLimits":
        """Create risk limits from application settings."""
        settings = get_settings()
        return cls(
            # Convert fractions to percentages (0.10 -> 10.0%)
            max_position_size_pct=settings.max_position_pct * 100,
            max_total_exposure_pct=50.0,
            max_daily_trades=settings.max_daily_trades,
            max_daily_loss=settings.daily_loss_limit,
            no_trading_before=settings.no_trading_before,
            no_trading_after=settings.no_trading_after,
            max_sector_exposure_pct=settings.max_sector_exposure * 100,
        )


@dataclass
class RiskCheckResult:
    """Result of a risk check."""
    
    passed: bool
    rule: str
    message: str
    severity: str = "warning"  # warning, block
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity,
        }


def risk_compliance_node(state: TradingState) -> dict[str, Any]:
    """
    LangGraph node for risk and compliance checks.
    
    Performs deterministic risk checks on validated signals.
    This is the final gatekeeper before trade execution.
    
    Args:
        state: Current trading state with validated signals
        
    Returns:
        State updates with approved and rejected trades
    """
    logger.info("Running Risk & Compliance Agent...")
    
    validated_signals = state.get("validated_signals", [])
    
    if not validated_signals:
        logger.info("No signals to check")
        return {
            "approved_trades": [],
            "risk_rejected": [],
            "risk_warnings": [],
            "trades_to_execute": [],
        }
    
    # Get current state
    portfolio = state.get("portfolio", {})
    daily_stats = state.get("daily_stats", {})
    
    # Get risk limits
    limits = RiskLimits.from_settings()
    
    approved = []
    rejected = []
    warnings = []
    
    for signal in validated_signals:
        checks = _run_risk_checks(signal, portfolio, daily_stats, limits)
        
        # Collect results
        blocking_failures = [c for c in checks if not c.passed and c.severity == "block"]
        warning_failures = [c for c in checks if not c.passed and c.severity == "warning"]
        
        if blocking_failures:
            # Reject the signal
            signal["risk_result"] = {
                "approved": False,
                "failures": [c.to_dict() for c in blocking_failures],
            }
            rejected.append(signal)
            
            for failure in blocking_failures:
                logger.warning(f"Trade rejected: {failure.message}")
        else:
            # Approve (possibly with warnings)
            signal["risk_result"] = {
                "approved": True,
                "warnings": [c.to_dict() for c in warning_failures],
            }
            approved.append(signal)
            
            for warning in warning_failures:
                warnings.append(warning.message)
                logger.info(f"Trade approved with warning: {warning.message}")
    
    logger.info(f"Risk check: {len(approved)} approved, {len(rejected)} rejected")
    
    return {
        "approved_trades": approved,
        "risk_rejected": rejected,
        "risk_warnings": warnings,
        "trades_to_execute": approved,  # Approved trades go to execution
    }


def _run_risk_checks(
    signal: dict[str, Any],
    portfolio: dict[str, Any],
    daily_stats: dict[str, Any],
    limits: RiskLimits,
) -> list[RiskCheckResult]:
    """Run all risk checks on a signal."""
    
    checks = []
    
    # 1. Daily trade limit
    trades_today = daily_stats.get("trades_count", 0)
    checks.append(RiskCheckResult(
        passed=trades_today < limits.max_daily_trades,
        rule="daily_trade_limit",
        message=f"Daily trade limit ({limits.max_daily_trades}) reached: {trades_today} trades today",
        severity="block",
    ))
    
    # 2. Daily loss limit
    daily_pnl = daily_stats.get("profit_loss", 0)
    checks.append(RiskCheckResult(
        passed=daily_pnl > -limits.max_daily_loss,
        rule="daily_loss_limit",
        message=f"Daily loss limit (₹{limits.max_daily_loss}) breached: ₹{abs(daily_pnl)} loss",
        severity="block",
    ))
    
    # 3. Position size limit
    position_pct = signal.get("position_size_pct", 0)
    checks.append(RiskCheckResult(
        passed=position_pct <= limits.max_position_size_pct,
        rule="position_size",
        message=f"Position size {position_pct}% exceeds limit of {limits.max_position_size_pct}%",
        severity="block",
    ))
    
    # 4. Risk-reward ratio
    rr_ratio = signal.get("risk_reward_ratio", 0)
    checks.append(RiskCheckResult(
        passed=rr_ratio >= limits.min_risk_reward,
        rule="risk_reward",
        message=f"Risk-reward {rr_ratio:.2f} below minimum {limits.min_risk_reward}",
        severity="warning",
    ))
    
    # 5. Stop loss percentage
    entry = signal.get("entry_price", 0)
    stop = signal.get("stop_loss", 0)
    if entry > 0:
        stop_pct = abs(entry - stop) / entry * 100
        checks.append(RiskCheckResult(
            passed=stop_pct <= limits.max_stop_loss_pct,
            rule="stop_loss_pct",
            message=f"Stop loss {stop_pct:.1f}% exceeds limit of {limits.max_stop_loss_pct}%",
            severity="warning",
        ))
    
    # 6. Max positions
    current_positions = len(portfolio.get("positions", []))
    checks.append(RiskCheckResult(
        passed=current_positions < limits.max_positions,
        rule="max_positions",
        message=f"Max positions ({limits.max_positions}) reached: {current_positions} open",
        severity="block",
    ))
    
    # 7. Trading hours (evaluated in IST, not host-local time)
    current_time = now_ist().strftime("%H:%M")
    in_trading_hours = limits.no_trading_before <= current_time <= limits.no_trading_after
    checks.append(RiskCheckResult(
        passed=in_trading_hours,
        rule="trading_hours",
        message=f"Outside trading hours ({limits.no_trading_before}-{limits.no_trading_after})",
        severity="block",
    ))
    
    # 8. Drawdown check
    max_dd = daily_stats.get("max_drawdown", 0)
    capital = portfolio.get("capital", 100000)
    dd_pct = (max_dd / capital * 100) if capital > 0 else 0
    checks.append(RiskCheckResult(
        passed=dd_pct < limits.max_drawdown_pct,
        rule="drawdown",
        message=f"Drawdown {dd_pct:.1f}% exceeds limit of {limits.max_drawdown_pct}%",
        severity="block",
    ))
    
    # 9. Duplicate position check
    symbol = signal.get("symbol", "")
    existing_positions = [p.get("symbol") for p in portfolio.get("positions", [])]
    checks.append(RiskCheckResult(
        passed=symbol not in existing_positions,
        rule="duplicate_position",
        message=f"Already have position in {symbol}",
        severity="warning",
    ))
    
    # 10. Confidence check
    confidence = signal.get("confidence", 0)
    validation_confidence = signal.get("validation", {}).get("confidence", 0)
    avg_confidence = (confidence + validation_confidence) / 2 if validation_confidence else confidence
    checks.append(RiskCheckResult(
        passed=avg_confidence >= 0.5,
        rule="confidence",
        message=f"Low confidence score: {avg_confidence:.2f}",
        severity="warning",
    ))
    
    # 11. Sector exposure check (NEW)
    new_symbol = signal.get("symbol", "")
    new_sector = get_stock_sector(new_symbol)
    sector_exposure = _calculate_sector_exposure(portfolio, new_sector, capital)
    checks.append(RiskCheckResult(
        passed=sector_exposure <= limits.max_sector_exposure_pct,
        rule="sector_exposure",
        message=f"Sector exposure ({new_sector}: {sector_exposure:.1f}%) exceeds limit of {limits.max_sector_exposure_pct}%",
        severity="warning",
    ))
    
    # 12. Correlated positions check (NEW)
    sector_positions = _count_sector_positions(portfolio, new_sector)
    checks.append(RiskCheckResult(
        passed=sector_positions < limits.max_correlated_positions,
        rule="correlated_positions",
        message=f"Too many positions in {new_sector} sector: {sector_positions}/{limits.max_correlated_positions}",
        severity="warning",
    ))
    
    return checks


def _calculate_sector_exposure(
    portfolio: dict[str, Any],
    sector: str,
    total_capital: float,
) -> float:
    """
    Calculate current exposure to a sector as percentage of capital.
    
    Args:
        portfolio: Current portfolio state
        sector: Sector to check
        total_capital: Total capital
        
    Returns:
        Sector exposure as percentage
    """
    if total_capital <= 0:
        return 0.0
    
    positions = portfolio.get("positions", [])
    sector_value = 0.0
    
    for position in positions:
        pos_symbol = position.get("symbol", "")
        pos_sector = get_stock_sector(pos_symbol)
        if pos_sector == sector:
            # Use market value or cost basis
            qty = position.get("quantity", 0)
            price = position.get("current_price", position.get("entry_price", 0))
            sector_value += qty * price
    
    return (sector_value / total_capital) * 100


def _count_sector_positions(portfolio: dict[str, Any], sector: str) -> int:
    """
    Count number of positions in a sector.
    
    Args:
        portfolio: Current portfolio state
        sector: Sector to count
        
    Returns:
        Number of positions in the sector
    """
    positions = portfolio.get("positions", [])
    count = 0
    
    for position in positions:
        pos_symbol = position.get("symbol", "")
        if get_stock_sector(pos_symbol) == sector:
            count += 1
    
    return count


def check_kill_switch(state: TradingState, limits: RiskLimits | None = None) -> bool:
    """
    Check if kill switch should be triggered.
    
    Returns True if trading should be halted immediately.
    """
    if limits is None:
        limits = RiskLimits.from_settings()
    
    daily_stats = state.get("daily_stats", {})
    
    # Check daily loss
    daily_pnl = daily_stats.get("profit_loss", 0)
    if daily_pnl <= -limits.max_daily_loss:
        logger.critical(f"KILL SWITCH: Daily loss limit breached (₹{abs(daily_pnl)})")
        return True
    
    # Check drawdown
    portfolio = state.get("portfolio", {})
    max_dd = daily_stats.get("max_drawdown", 0)
    capital = portfolio.get("capital", 100000)
    dd_pct = (max_dd / capital * 100) if capital > 0 else 0
    
    if dd_pct >= limits.max_drawdown_pct:
        logger.critical(f"KILL SWITCH: Drawdown limit breached ({dd_pct:.1f}%)")
        return True
    
    return False
