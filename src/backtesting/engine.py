"""
Backtesting Engine

Simple backtesting framework for testing trading strategies on historical data.
Uses lightweight implementation without heavy dependencies.

Features:
- Run strategies on historical OHLCV data
- Calculate performance metrics (Sharpe, Max Drawdown)
- Compare multiple strategies
- Generate reports
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.execution.costs import CostModel
from src.market.yfinance_feed import YFinanceFeed

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """A single trade executed during backtest."""

    entry_date: datetime
    exit_date: datetime | None
    symbol: str
    side: str  # "LONG" or "SHORT"
    entry_price: float
    exit_price: float = 0.0
    quantity: int = 1
    pnl: float = 0.0
    pnl_pct: float = 0.0

    def close(self, exit_date: datetime, exit_price: float):
        """Close the trade and calculate P&L."""
        self.exit_date = exit_date
        self.exit_price = exit_price

        if self.side == "LONG":
            self.pnl = (exit_price - self.entry_price) * self.quantity
        else:
            self.pnl = (self.entry_price - exit_price) * self.quantity

        self.pnl_pct = (self.pnl / (self.entry_price * self.quantity)) * 100


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    strategy_name: str
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float  # expected P&L per trade (the trade-by-trade edge)
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "symbol": self.symbol,
            "period": f"{self.start_date} to {self.end_date}",
            "initial_capital": self.initial_capital,
            "final_capital": round(self.final_capital, 2),
            "total_return": round(self.total_return, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
        }

    def print_summary(self):
        """Print a formatted summary."""
        print("\n" + "=" * 50)
        print(f"BACKTEST RESULTS: {self.strategy_name}")
        print("=" * 50)
        print(f"Symbol: {self.symbol}")
        print(f"Period: {self.start_date} to {self.end_date}")
        print("-" * 50)
        print(f"Initial Capital: ₹{self.initial_capital:,.2f}")
        print(f"Final Capital:   ₹{self.final_capital:,.2f}")
        print(f"Total Return:    ₹{self.total_return:+,.2f} ({self.total_return_pct:+.2f}%)")
        print("-" * 50)
        print(f"Total Trades:    {self.total_trades}")
        print(f"Winning Trades:  {self.winning_trades}")
        print(f"Losing Trades:   {self.losing_trades}")
        print(f"Win Rate:        {self.win_rate:.1f}%")
        print(f"Avg Win:         ₹{self.avg_win:,.2f}")
        print(f"Avg Loss:        ₹{self.avg_loss:,.2f}")
        print(f"Profit Factor:   {self.profit_factor:.2f}")
        print(f"Expectancy:      ₹{self.expectancy:+,.2f} per trade")
        print("-" * 50)
        print(f"Max Drawdown:    ₹{self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)")
        print(f"Sharpe Ratio:    {self.sharpe_ratio:.2f}")
        print("=" * 50)


class Strategy:
    """Base strategy class."""

    name: str = "BaseStrategy"

    def __init__(self):
        self.position = 0  # 1 = long, -1 = short, 0 = flat
        self.entry_price = 0.0
        self.entry_date = None

    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        """
        Called on each bar of data.

        Args:
            row: Current bar (Open, High, Low, Close, Volume)
            history: Historical data up to this point

        Returns:
            "BUY", "SELL", or None
        """
        raise NotImplementedError


class BacktestEngine:
    """
    Simple backtesting engine.

    Runs a strategy on historical data and calculates metrics.
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission: float = 0.0003,  # 0.03% per trade (used only when cost_model is None)
        slippage: float = 0.0001,  # 0.01% slippage (used only when cost_model is None)
        cost_model: "CostModel | None" = None,
    ):
        """
        Initialize backtest engine.

        Args:
            initial_capital: Starting capital in INR
            commission: Commission per trade (fraction)
            slippage: Slippage per trade (fraction)
        """
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        # When provided, the audited CostModel (realistic NSE slippage + fees) is used for
        # fills and charges instead of the flat commission/slippage above.
        self.cost_model = cost_model

    def fetch_data(
        self,
        symbol: str,
        period: str = "1y",
    ) -> pd.DataFrame | None:
        """Fetch historical data for backtesting."""
        feed = YFinanceFeed(symbols=[symbol])
        return feed.get_historical(symbol, period=period)

    def run(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        symbol: str = "UNKNOWN",
    ) -> BacktestResult:
        """
        Run backtest on historical data.

        Args:
            strategy: Strategy instance
            data: OHLCV DataFrame with DatetimeIndex
            symbol: Stock symbol

        Returns:
            BacktestResult with metrics
        """
        if data is None or data.empty:
            raise ValueError("No data provided for backtest")

        # Ensure required columns
        required = ["Open", "High", "Low", "Close", "Volume"]
        if not all(col in data.columns for col in required):
            raise ValueError(f"Data must have columns: {required}")

        # Initialize
        capital = self.initial_capital
        position = 0
        entry_price = 0.0
        entry_date = None

        trades: list[Trade] = []
        equity_curve = [capital]

        # Minimum lookback
        lookback = 20

        # Iterate through data
        for i in range(lookback, len(data)):
            row = data.iloc[i]
            history = data.iloc[:i]
            current_price = row["Close"]
            current_date = data.index[i]

            # Get signal
            signal = strategy.on_bar(row, history)

            # Process signal
            if signal == "BUY" and position == 0:
                # Enter long
                position = 1
                if self.cost_model is not None:
                    entry_price = self.cost_model.fill_price(current_price, "BUY")
                    commission_cost = self.cost_model.charges(entry_price, "BUY")
                else:
                    entry_price = current_price * (1 + self.slippage)
                    commission_cost = entry_price * self.commission
                entry_date = current_date
                capital -= commission_cost

            elif signal == "SELL" and position == 1:
                # Exit long
                if self.cost_model is not None:
                    exit_price = self.cost_model.fill_price(current_price, "SELL")
                    commission_cost = self.cost_model.charges(exit_price, "SELL")
                else:
                    exit_price = current_price * (1 - self.slippage)
                    commission_cost = exit_price * self.commission

                trade = Trade(
                    entry_date=entry_date,
                    exit_date=current_date,
                    symbol=symbol,
                    side="LONG",
                    entry_price=entry_price,
                    exit_price=exit_price,
                )
                trade.close(current_date, exit_price)
                trades.append(trade)

                capital += trade.pnl - commission_cost
                position = 0

            # Update equity curve
            if position == 1:
                unrealized = (current_price - entry_price) * 1
                equity_curve.append(capital + unrealized)
            else:
                equity_curve.append(capital)

        # Close any open position at end
        if position == 1:
            exit_price = data.iloc[-1]["Close"]
            trade = Trade(
                entry_date=entry_date,
                exit_date=data.index[-1],
                symbol=symbol,
                side="LONG",
                entry_price=entry_price,
                exit_price=exit_price,
            )
            trade.close(data.index[-1], exit_price)
            trades.append(trade)
            capital += trade.pnl

        # Calculate metrics
        return self._calculate_metrics(
            strategy_name=strategy.name,
            symbol=symbol,
            data=data,
            trades=trades,
            equity_curve=equity_curve,
            final_capital=capital,
        )

    def _calculate_metrics(
        self,
        strategy_name: str,
        symbol: str,
        data: pd.DataFrame,
        trades: list[Trade],
        equity_curve: list[float],
        final_capital: float,
    ) -> BacktestResult:
        """Calculate performance metrics."""

        # Basic stats
        total_return = final_capital - self.initial_capital
        total_return_pct = (total_return / self.initial_capital) * 100

        # Trade stats
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl < 0]

        win_rate = (len(winning) / len(trades) * 100) if trades else 0
        avg_win = np.mean([t.pnl for t in winning]) if winning else 0
        avg_loss = np.mean([t.pnl for t in losing]) if losing else 0

        gross_profit = sum(t.pnl for t in winning)
        gross_loss = abs(sum(t.pnl for t in losing))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # Expectancy: average P&L per trade (the realised edge).
        expectancy = (sum(t.pnl for t in trades) / len(trades)) if trades else 0.0

        # Drawdown
        equity = np.array(equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        max_drawdown = np.max(drawdown)
        max_drawdown_pct = (max_drawdown / np.max(peak)) * 100 if np.max(peak) > 0 else 0

        # Sharpe Ratio (annualized, assuming 252 trading days)
        returns = np.diff(equity) / equity[:-1]
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(252)
        else:
            sharpe = 0

        return BacktestResult(
            strategy_name=strategy_name,
            symbol=symbol,
            start_date=str(data.index[0].date()),
            end_date=str(data.index[-1].date()),
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            total_return_pct=total_return_pct,
            total_trades=len(trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            expectancy=expectancy,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe,
            trades=trades,
            equity_curve=equity_curve,
        )


def compare_results(baseline: BacktestResult, candidate: BacktestResult) -> dict[str, Any]:
    """
    Before/after scorecard: deltas of key metrics between two backtests.

    Lets a change be *proven* to help (or not) before trusting it — e.g. baseline vs a
    tweaked strategy/prompt/sizing run on the same data. Positive `improved` means the
    candidate beat the baseline on net (return up, drawdown down).
    """

    def _delta(metric: str) -> float:
        return round(float(getattr(candidate, metric)) - float(getattr(baseline, metric)), 4)

    deltas = {
        "total_return_pct": _delta("total_return_pct"),
        "win_rate": _delta("win_rate"),
        "profit_factor": _delta("profit_factor"),
        "expectancy": _delta("expectancy"),
        "sharpe_ratio": _delta("sharpe_ratio"),
        "max_drawdown_pct": _delta("max_drawdown_pct"),  # lower is better
    }
    improved = (
        deltas["total_return_pct"] > 0
        and deltas["expectancy"] >= 0
        and deltas["max_drawdown_pct"] <= 0
    )
    return {
        "baseline": baseline.strategy_name,
        "candidate": candidate.strategy_name,
        "deltas": deltas,
        "improved": improved,
    }


def test_backtest_engine():
    """Test the backtest engine."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    from src.backtesting.strategies import MomentumStrategy

    print("=" * 60)
    print("[BACKTEST] RakshaQuant - Backtest Engine Test")
    print("=" * 60)

    engine = BacktestEngine(initial_capital=100000)

    print("\n[DATA] Fetching RELIANCE 1-year history...")
    data = engine.fetch_data("RELIANCE", period="1y")

    if data is None or data.empty:
        print("  ❌ Failed to fetch data")
        return

    print(f"  ✅ Loaded {len(data)} bars")

    print("\n[RUN] Running Momentum Strategy backtest...")
    strategy = MomentumStrategy()
    result = engine.run(strategy, data, symbol="RELIANCE")

    result.print_summary()

    print("\n" + "=" * 60)
    print("[SUCCESS] Backtest complete!")
    print("=" * 60)


if __name__ == "__main__":
    test_backtest_engine()
