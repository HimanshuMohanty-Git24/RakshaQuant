"""
Trading Strategies for Backtesting

Pre-built strategies that can be backtested on historical data.

Strategies:
- MomentumStrategy: Buy on upward momentum, sell on reversal
- MeanReversionStrategy: Buy oversold, sell overbought
- SMAcrossoverStrategy: Classic moving average crossover
"""

import numpy as np
import pandas as pd

from src.market.indicators import Timeframe, calculate_indicators
from src.market.signals import SignalEngine, SignalType


class Strategy:
    """Base strategy class."""

    name: str = "BaseStrategy"

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


class RealSignalStrategy(Strategy):
    """
    Backtests the SAME logic the live system trades.

    Instead of a separate re-implementation of indicators, this feeds the real
    `ta`-based indicators (src.market.indicators) into the live `SignalEngine`, so a
    backtest faithfully reflects live behaviour (fixes the audit's backtest≠live divergence).
    Only uses bars strictly prior to the current one (no look-ahead).
    """

    name = "RealSignal"

    def __init__(self, symbol: str = "UNKNOWN", min_bars: int = 50):
        self.symbol = symbol
        self.min_bars = min_bars
        self._engine = SignalEngine()

    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        if len(history) < self.min_bars:
            return None
        # The live indicators expect lowercase OHLCV column names.
        df = history.rename(columns=str.lower)
        try:
            indicators = calculate_indicators(df, self.symbol, timeframe=Timeframe.D1)
            signals = self._engine.generate_signals(indicators)
        except Exception:
            return None
        if not signals:
            return None
        top = signals[0]
        if top.signal_type == SignalType.BUY:
            return "BUY"
        if top.signal_type == SignalType.SELL:
            return "SELL"
        return None


class MomentumStrategy(Strategy):
    """
    Momentum-based strategy.
    
    Buy when price shows strong upward momentum.
    Sell when momentum reverses.
    
    Signals:
    - BUY: Price > 20-day SMA AND RSI crosses above 50
    - SELL: Price < 20-day SMA OR RSI crosses below 50
    """
    
    name = "Momentum"
    
    def __init__(
        self,
        sma_period: int = 20,
        rsi_period: int = 14,
        rsi_buy: int = 50,
        rsi_sell: int = 50,
    ):
        self.sma_period = sma_period
        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.in_position = False
    
    def _calculate_rsi(self, prices: pd.Series) -> float:
        """Calculate RSI."""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        
        if loss.iloc[-1] == 0:
            return 100
        
        rs = gain.iloc[-1] / loss.iloc[-1]
        return 100 - (100 / (1 + rs))
    
    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        if len(history) < self.sma_period:
            return None
        
        close = row["Close"]
        closes = history["Close"]
        
        # Calculate indicators
        sma = closes.tail(self.sma_period).mean()
        rsi = self._calculate_rsi(closes)
        
        # Generate signals
        if not self.in_position:
            if close > sma and rsi > self.rsi_buy:
                self.in_position = True
                return "BUY"
        else:
            if close < sma or rsi < self.rsi_sell:
                self.in_position = False
                return "SELL"
        
        return None


class MeanReversionStrategy(Strategy):
    """
    Mean reversion strategy.
    
    Buy when price is oversold (below lower Bollinger Band).
    Sell when price reaches mean or overbought.
    
    Signals:
    - BUY: Price < Lower BB AND RSI < 30
    - SELL: Price > Middle BB OR RSI > 70
    """
    
    name = "MeanReversion"
    
    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: int = 30,
        rsi_overbought: int = 70,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.in_position = False
    
    def _calculate_rsi(self, prices: pd.Series) -> float:
        """Calculate RSI."""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        
        if loss.iloc[-1] == 0:
            return 100
        
        rs = gain.iloc[-1] / loss.iloc[-1]
        return 100 - (100 / (1 + rs))
    
    def _calculate_bollinger(self, prices: pd.Series) -> tuple[float, float, float]:
        """Calculate Bollinger Bands."""
        sma = prices.tail(self.bb_period).mean()
        std = prices.tail(self.bb_period).std()
        
        upper = sma + (self.bb_std * std)
        lower = sma - (self.bb_std * std)
        
        return lower, sma, upper
    
    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        if len(history) < self.bb_period:
            return None
        
        close = row["Close"]
        closes = history["Close"]
        
        # Calculate indicators
        lower_bb, middle_bb, upper_bb = self._calculate_bollinger(closes)
        rsi = self._calculate_rsi(closes)
        
        # Generate signals
        if not self.in_position:
            if close < lower_bb and rsi < self.rsi_oversold:
                self.in_position = True
                return "BUY"
        else:
            if close > middle_bb or rsi > self.rsi_overbought:
                self.in_position = False
                return "SELL"
        
        return None


class SMACrossoverStrategy(Strategy):
    """
    Simple Moving Average Crossover.
    
    Classic trend-following strategy.
    
    Signals:
    - BUY: Fast SMA crosses above Slow SMA
    - SELL: Fast SMA crosses below Slow SMA
    """
    
    name = "SMA_Crossover"
    
    def __init__(
        self,
        fast_period: int = 10,
        slow_period: int = 30,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.in_position = False
        self.prev_fast = None
        self.prev_slow = None
    
    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        if len(history) < self.slow_period:
            return None
        
        closes = history["Close"]
        
        # Calculate SMAs
        fast_sma = closes.tail(self.fast_period).mean()
        slow_sma = closes.tail(self.slow_period).mean()
        
        signal = None
        
        # Check for crossover
        if self.prev_fast is not None and self.prev_slow is not None:
            # Golden cross (fast crosses above slow)
            if self.prev_fast <= self.prev_slow and fast_sma > slow_sma:
                if not self.in_position:
                    self.in_position = True
                    signal = "BUY"
            
            # Death cross (fast crosses below slow)
            elif self.prev_fast >= self.prev_slow and fast_sma < slow_sma:
                if self.in_position:
                    self.in_position = False
                    signal = "SELL"
        
        self.prev_fast = fast_sma
        self.prev_slow = slow_sma
        
        return signal


class RSIStrategy(Strategy):
    """
    RSI-based strategy.
    
    Signals:
    - BUY: RSI crosses above oversold level
    - SELL: RSI crosses above overbought level
    """
    
    name = "RSI"
    
    def __init__(
        self,
        rsi_period: int = 14,
        oversold: int = 30,
        overbought: int = 70,
    ):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.in_position = False
        self.prev_rsi = None
    
    def _calculate_rsi(self, prices: pd.Series) -> float:
        """Calculate RSI."""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0).rolling(self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(self.rsi_period).mean()
        
        if loss.iloc[-1] == 0:
            return 100
        
        rs = gain.iloc[-1] / loss.iloc[-1]
        return 100 - (100 / (1 + rs))
    
    def on_bar(self, row: pd.Series, history: pd.DataFrame) -> str | None:
        if len(history) < self.rsi_period + 5:
            return None
        
        closes = history["Close"]
        rsi = self._calculate_rsi(closes)
        
        signal = None
        
        if self.prev_rsi is not None:
            # RSI crosses above oversold
            if self.prev_rsi <= self.oversold and rsi > self.oversold:
                if not self.in_position:
                    self.in_position = True
                    signal = "BUY"
            
            # RSI crosses above overbought
            elif rsi > self.overbought:
                if self.in_position:
                    self.in_position = False
                    signal = "SELL"
        
        self.prev_rsi = rsi
        return signal
