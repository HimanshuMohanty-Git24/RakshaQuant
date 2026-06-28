"""
Price Prediction Agent

Uses ensemble machine learning to predict next candle direction.
Based on RandomForest and Linear Regression with technical features.

Features:
- Direction prediction (up/down)
- Confidence scoring from model agreement
- Historical pattern analysis
- Ensemble voting from multiple models
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

try:
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import LinearRegression
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    LinearRegression = None
    RandomForestRegressor = None
    GradientBoostingRegressor = None
    StandardScaler = None

from src.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class PredictionSignal:
    """Price prediction signal."""

    symbol: str
    direction: str  # "up" or "down"
    confidence: float  # 0-1
    predicted_change_pct: float
    reasoning: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": self.confidence,
            "predicted_change_pct": self.predicted_change_pct,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp.isoformat(),
        }


class PredictionAgent:
    """
    Predicts price direction using ensemble ML.

    Uses multiple models (LinearRegression, RandomForest, GradientBoosting)
    and combines their predictions for more robust forecasting.
    """

    def __init__(self, lookback_periods: int = 20):
        """
        Initialize prediction agent.

        Args:
            lookback_periods: Number of candles to use for prediction
        """
        self.lookback = lookback_periods
        self.settings = get_settings()

    def _create_features(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """
        Create features from OHLCV data for ML.

        Features:
        - Price changes (returns)
        - Moving average ratios (multiple periods)
        - Volume changes
        - Price range metrics
        - RSI-like momentum
        """
        if len(df) < self.lookback + 5:
            return None, None

        # Calculate features
        df = df.copy()
        df["returns"] = df["Close"].pct_change()
        df["returns_2"] = df["Close"].pct_change(2)
        df["returns_5"] = df["Close"].pct_change(5)

        df["sma_5"] = df["Close"].rolling(5).mean()
        df["sma_10"] = df["Close"].rolling(10).mean()
        df["sma_20"] = df["Close"].rolling(20).mean()
        df["sma_ratio_5_10"] = df["sma_5"] / df["sma_10"]
        df["sma_ratio_10_20"] = df["sma_10"] / df["sma_20"]

        df["vol_change"] = df["Volume"].pct_change()
        df["vol_sma_5"] = df["Volume"].rolling(5).mean()
        df["vol_ratio"] = df["Volume"] / df["vol_sma_5"]

        df["high_low_range"] = (df["High"] - df["Low"]) / df["Close"]
        df["close_position"] = (df["Close"] - df["Low"]) / (df["High"] - df["Low"] + 0.0001)

        # RSI-like momentum
        delta = df["Close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 0.0001)
        df["rsi"] = 100 - (100 / (1 + rs))
        df["rsi_normalized"] = df["rsi"] / 100

        # Target: next day return direction
        df["target"] = df["returns"].shift(-1)

        # Drop NaN
        df = df.dropna()

        if len(df) < 10:
            return None, None

        # Feature columns
        feature_cols = [
            "returns",
            "returns_2",
            "returns_5",
            "sma_ratio_5_10",
            "sma_ratio_10_20",
            "vol_change",
            "vol_ratio",
            "high_low_range",
            "close_position",
            "rsi_normalized",
        ]
        X = df[feature_cols].values
        y = df["target"].values

        return X, y

    def predict(
        self,
        historical_data: pd.DataFrame | dict[str, Any],
        symbol: str = "UNKNOWN",
    ) -> PredictionSignal:
        """
        Predict next candle direction using ensemble ML.

        Args:
            historical_data: DataFrame with OHLCV columns or dict
            symbol: Stock symbol

        Returns:
            PredictionSignal with direction and confidence
        """
        if not SKLEARN_AVAILABLE:
            logger.warning("scikit-learn not available, using fallback prediction")
            return self._fallback_predict(historical_data, symbol)

        try:
            # Convert dict to DataFrame if needed
            if isinstance(historical_data, dict):
                df = pd.DataFrame(historical_data)
            else:
                df = historical_data.copy()

            # Ensure required columns
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            if not all(col in df.columns for col in required_cols):
                logger.warning(f"Missing columns for {symbol}, using fallback")
                return self._fallback_predict(historical_data, symbol)

            # Create features
            X, y = self._create_features(df)

            if X is None or len(X) < 10:
                logger.warning(f"Insufficient data for {symbol}, using fallback")
                return self._fallback_predict(historical_data, symbol)

            # Train/test split (use last 5 for validation)
            X_train, y_train = X[:-5], y[:-5]
            X_test, y_test = X[-5:], y[-5:]

            # Scale features
            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_test_scaled = scaler.transform(X_test)

            # Ensemble of models
            models = {
                "linear": LinearRegression(),
                "random_forest": RandomForestRegressor(
                    n_estimators=50,
                    max_depth=5,
                    random_state=42,
                    n_jobs=-1,
                ),
                "gradient_boost": GradientBoostingRegressor(
                    n_estimators=50,
                    max_depth=3,
                    random_state=42,
                ),
            }

            predictions = {}
            scores = {}

            for name, model in models.items():
                try:
                    # Train model
                    model.fit(X_train_scaled, y_train)

                    # Predict on last data point
                    last_features = X[-1:].reshape(1, -1)
                    last_scaled = scaler.transform(last_features)
                    pred = model.predict(last_scaled)[0]

                    # Calculate R² on test set
                    r2 = model.score(X_test_scaled, y_test)

                    predictions[name] = pred
                    scores[name] = max(0.0, r2)  # Clamp negative R²
                except Exception as e:
                    logger.warning(f"Model {name} failed: {e}")

            if not predictions:
                return self._fallback_predict(historical_data, symbol)

            # Weighted ensemble prediction
            total_weight = sum(scores.values()) + 0.0001
            ensemble_pred = (
                sum(pred * scores.get(name, 0) for name, pred in predictions.items()) / total_weight
            )

            # Calculate confidence from model agreement
            pred_directions = [1 if p > 0 else -1 for p in predictions.values()]
            agreement = abs(sum(pred_directions)) / len(pred_directions)
            avg_r2 = sum(scores.values()) / len(scores)
            confidence = max(0.3, min(0.9, (agreement + avg_r2) / 2 + 0.2))

            # Determine direction
            direction = "up" if ensemble_pred > 0 else "down"

            # Build reasoning
            recent_returns = df["Close"].pct_change().tail(5).mean() * 100
            trend = "upward" if recent_returns > 0 else "downward"
            model_votes = f"{sum(1 for d in pred_directions if d > 0)}/{len(pred_directions)} models predict up"

            reasoning = (
                f"Ensemble of {len(predictions)} models on {len(X)} data points. "
                f"Recent trend: {trend} ({recent_returns:+.2f}% avg). "
                f"{model_votes}. Predicted move: {abs(ensemble_pred) * 100:.2f}% {direction}."
            )

            logger.info(f"Prediction for {symbol}: {direction} ({confidence:.1%} confidence)")

            return PredictionSignal(
                symbol=symbol,
                direction=direction,
                confidence=confidence,
                predicted_change_pct=ensemble_pred * 100,
                reasoning=reasoning,
            )

        except Exception as e:
            logger.error(f"Prediction error for {symbol}: {e}")
            return self._fallback_predict(historical_data, symbol)

    def _fallback_predict(
        self,
        data: Any,
        symbol: str,
    ) -> PredictionSignal:
        """Fallback prediction using simple momentum."""
        try:
            if isinstance(data, pd.DataFrame) and "Close" in data.columns:
                recent_change = data["Close"].pct_change().tail(3).mean()
            elif isinstance(data, dict) and "close" in data:
                closes = data["close"]
                if len(closes) >= 3:
                    recent_change = (closes[-1] - closes[-3]) / closes[-3]
                else:
                    recent_change = 0
            else:
                recent_change = 0
        except Exception:
            recent_change = 0

        direction = "up" if recent_change > 0 else "down"

        return PredictionSignal(
            symbol=symbol,
            direction=direction,
            confidence=0.4,  # Low confidence for fallback
            predicted_change_pct=recent_change * 100,
            reasoning=f"Fallback momentum prediction based on recent trend ({recent_change * 100:+.2f}%)",
        )


def prediction_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    LangGraph node for price prediction.

    Generates prediction signals for the symbols in the *raw* signals. This node
    runs as a support agent (before regime/validation), so ``validated_signals``
    is still empty at this point — sourcing from it produced zero predictions.
    Raw ``signals`` are already populated by the time the graph runs, so the
    regime and validation agents now receive real ML consensus.
    """
    agent = PredictionAgent()

    # Get historical data from market data manager (if available)
    from src.market.yfinance_feed import YFinanceFeed

    signals = state.get("signals", [])
    predictions = []
    seen_symbols: set[str] = set()

    for signal in signals:
        symbol = signal.get("symbol", "")
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        if len(seen_symbols) > 3:  # Limit to top 3 distinct symbols
            break

        try:
            # Fetch historical data
            feed = YFinanceFeed(symbols=[symbol])
            hist = feed.get_historical(symbol, period="1mo")

            if hist is not None and not hist.empty:
                pred = agent.predict(hist, symbol)
                predictions.append(pred.to_dict())
        except Exception as e:
            logger.warning(f"Could not generate prediction for {symbol}: {e}")

    return {
        "prediction_signals": predictions,
    }


def test_prediction_agent():
    """Test the prediction agent."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[PREDICTION] RakshaQuant - Price Prediction Test")
    print("=" * 60)

    agent = PredictionAgent()

    # Create sample data
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=50, freq="D")

    # Simulate uptrend
    base_price = 100
    prices = [base_price]
    for i in range(49):
        change = np.random.normal(0.002, 0.02)
        prices.append(prices[-1] * (1 + change))

    df = pd.DataFrame(
        {
            "Open": [p * 0.999 for p in prices],
            "High": [p * 1.01 for p in prices],
            "Low": [p * 0.99 for p in prices],
            "Close": prices,
            "Volume": np.random.randint(100000, 1000000, 50),
        },
        index=dates,
    )

    print("\n[DATA] Generated sample data:")
    print(f"  Start price: ₹{prices[0]:.2f}")
    print(f"  End price: ₹{prices[-1]:.2f}")
    print(f"  Total return: {(prices[-1] / prices[0] - 1) * 100:.2f}%")

    print("\n[PREDICT] Running prediction...")
    prediction = agent.predict(df, "SAMPLE")

    print(f"\n  Direction: {prediction.direction.upper()}")
    print(f"  Confidence: {prediction.confidence:.1%}")
    print(f"  Predicted change: {prediction.predicted_change_pct:+.2f}%")
    print(f"  Reasoning: {prediction.reasoning}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Prediction agent working!")
    print("=" * 60)


if __name__ == "__main__":
    test_prediction_agent()
