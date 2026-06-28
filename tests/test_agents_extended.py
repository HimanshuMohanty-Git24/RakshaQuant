from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

# Mock settings
with patch("src.config.get_settings") as mock_get_settings:
    mock_settings = MagicMock()
    mock_settings.groq_api_key.get_secret_value.return_value = "token"
    mock_settings.groq_model_fallback = "llama"
    mock_get_settings.return_value = mock_settings

    from src.agents.graph import (
        create_trading_graph,
        run_trading_cycle,
        should_continue_after_regime,
        should_continue_after_validation,
    )
    from src.agents.news_analyst import NewsAnalyst, NewsItem, NewsSentiment
    from src.agents.prediction import PredictionAgent, PredictionSignal
    from src.agents.sentiment import MarketSentimentAgent
    from src.agents.state import create_initial_state

# --- NewsAnalyst Tests ---


@pytest.fixture
def news_analyst():
    # Settings mocked at import level, but we might need to mock again if instantiating calls get_settings
    with patch("src.agents.news_analyst.get_settings") as mock_settings:
        mock_settings.return_value.groq_api_key.get_secret_value.return_value = "token"
        mock_settings.return_value.groq_model_fallback = "llama"
        return NewsAnalyst()


def test_fetch_news(news_analyst):
    with patch("src.agents.news_analyst.feedparser.parse") as mock_parse:
        mock_parse.return_value.entries = [
            {"title": "Stock Up - Source", "published": "now", "link": "http://link"}
        ]

        items = news_analyst.fetch_news("query")
        assert len(items) == 1
        assert items[0].title == "Stock Up"
        assert items[0].source == "Source"


@pytest.mark.asyncio
async def test_analyze_sentiment(news_analyst):
    with patch("src.agents.news_analyst.ChatGroq") as mock_llm_cls:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = '{"sentiment": 0.8, "reasoning": "Good news"}'
        news_analyst._llm = mock_llm

        score, reason = await news_analyst.analyze_sentiment(["Headline 1"])
        assert score == 0.8
        assert reason == "Good news"


@pytest.mark.asyncio
async def test_get_sentiment(news_analyst):
    with (
        patch.object(news_analyst, "fetch_news") as mock_fetch,
        patch.object(news_analyst, "analyze_sentiment", new_callable=AsyncMock) as mock_analyze,
    ):
        mock_fetch.return_value = [NewsItem("Title", "Source", "Time", "Link")]
        mock_analyze.return_value = (0.5, "Bullish")

        sentiment = await news_analyst.get_sentiment("query")

        assert sentiment.avg_sentiment == 0.5
        assert sentiment.sentiment_label == "bullish"
        assert sentiment.items[0].sentiment_score == 0.5


@pytest.mark.asyncio
async def test_get_stock_sentiment(news_analyst):
    with patch.object(news_analyst, "get_sentiment", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = NewsSentiment("q", [], 0.0)
        await news_analyst.get_stock_sentiment("AAPL")
        mock_get.assert_called_with("AAPL stock NSE India")


# --- PredictionAgent Tests ---


@pytest.fixture
def prediction_agent():
    return PredictionAgent()


def test_create_features(prediction_agent):
    df = pd.DataFrame(
        {"Close": [100] * 30, "Volume": [1000] * 30, "High": [101] * 30, "Low": [99] * 30}
    )

    X, y = prediction_agent._create_features(df)
    assert X is not None
    assert len(X) > 0


def test_predict_sklearn(prediction_agent):
    df = pd.DataFrame(
        {
            "Open": [100] * 30,
            "High": [101] * 30,
            "Low": [99] * 30,
            "Close": [100 + i for i in range(30)],  # Uptrend
            "Volume": [1000] * 30,
        }
    )

    with patch("src.agents.prediction.SKLEARN_AVAILABLE", True):
        # We need to ensure sklearn is actually importable or mocked if not
        try:
            from sklearn.linear_model import LinearRegression

            signal = prediction_agent.predict(df, "AAPL")
            assert signal.symbol == "AAPL"
            assert signal.confidence >= 0.3
        except ImportError:
            pass  # Skip if sklearn not installed in test env (it is installed in dev env)


def test_predict_fallback(prediction_agent):
    data = {"close": [100, 101, 102]}
    with patch("src.agents.prediction.SKLEARN_AVAILABLE", False):
        signal = prediction_agent.predict(data, "AAPL")
        assert signal.direction == "up"
        assert signal.confidence == 0.4


def test_prediction_node():
    from src.agents.prediction import prediction_node

    # prediction_node now sources from raw `signals` (available at the support-agent
    # stage), not `validated_signals` (which is still empty when this node runs).
    state = {"signals": [{"symbol": "AAPL"}]}

    # Mock YFinanceFeed where it is imported (inside the function)
    # Since we can't easily mock local imports with patch, we will mock sys.modules
    with patch("src.market.yfinance_feed.YFinanceFeed") as MockFeed:
        mock_feed_instance = MockFeed.return_value
        mock_feed_instance.get_historical.return_value = pd.DataFrame(
            {
                "Open": [100] * 30,
                "High": [101] * 30,
                "Low": [99] * 30,
                "Close": [100] * 30,
                "Volume": [1000] * 30,
            }
        )

        # We also need to patch PredictionAgent.predict to avoid re-running logic
        with patch("src.agents.prediction.PredictionAgent.predict") as mock_predict:
            mock_predict.return_value = PredictionSignal("AAPL", "up", 0.8, 1.0, "reason")

            result = prediction_node(state)
            assert len(result["prediction_signals"]) == 1


# --- MarketSentimentAgent Tests ---


@pytest.fixture
def sentiment_agent():
    return MarketSentimentAgent()


def test_calculate_volatility_score(sentiment_agent):
    score = sentiment_agent.calculate_volatility_score(15.0, 15.0)
    assert score == 45.0  # Ratio 1.0 -> 45.0

    score = sentiment_agent.calculate_volatility_score(30.0, 15.0)
    assert score == 10.0  # Ratio 2.0 -> 10.0


def test_calculate_breadth_score(sentiment_agent):
    score = sentiment_agent.calculate_breadth_score(10, 5)
    assert abs(score - 0.33) < 0.01


def test_calculate_mood_index(sentiment_agent):
    # news=1.0 (100), vol=50, breadth=1.0 (100)
    # 100*0.35 + 50*0.35 + 100*0.3 = 35 + 17.5 + 30 = 82.5
    idx = sentiment_agent.calculate_mood_index(1.0, 50, 1.0)
    assert idx == 82


def test_analyze(sentiment_agent):
    market_data = {"A": {"change_percent": 1.0}, "B": {"change_percent": -0.5}}

    signal = sentiment_agent.analyze(
        news_sentiment=0.5,
        market_data=market_data,
        volatility=15.0,  # High vol -> low score
    )

    assert signal.mood_index > 0
    assert signal.confidence > 0


def test_sentiment_analysis_node():
    from src.agents.sentiment import sentiment_analysis_node

    state = {
        "news_sentiment": {"avg_sentiment": 0.5},
        "market_data": {"A": {"change_percent": 1.0}},
    }

    result = sentiment_analysis_node(state)
    assert "market_mood" in result


# --- Graph Tests ---


def test_should_continue_after_regime():
    # Kill switch
    state = create_initial_state()
    state["portfolio"]["capital"] = 100000
    state["daily_stats"]["profit_loss"] = -50000  # Big loss

    # We need to mock risk limits inside check_kill_switch called by should_continue_after_regime
    with patch("src.agents.graph.check_kill_switch", return_value=True):
        assert should_continue_after_regime(state) == "end"

    with patch("src.agents.graph.check_kill_switch", return_value=False):
        # Low confidence
        state["regime_confidence"] = 0.1
        assert should_continue_after_regime(state) == "end"

        # High confidence
        state["regime_confidence"] = 0.8
        assert should_continue_after_regime(state) == "strategy_selection"


def test_should_continue_after_validation():
    state = create_initial_state()

    state["validated_signals"] = []
    assert should_continue_after_validation(state) == "end"

    state["validated_signals"] = [{"id": 1}]
    assert should_continue_after_validation(state) == "risk_compliance"


def test_create_trading_graph():
    graph = create_trading_graph(with_memory=False)
    assert graph is not None


@pytest.mark.asyncio
async def test_run_trading_cycle():
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={"approved_trades": []})

    result = await run_trading_cycle(graph, {}, {}, [])
    assert "approved_trades" in result


def test_get_graph_visualization():
    graph = MagicMock()
    graph.get_graph.return_value.draw_mermaid.return_value = "graph"

    from src.agents.graph import get_graph_visualization

    vis = get_graph_visualization(graph)
    assert vis == "graph"
