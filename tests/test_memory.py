import datetime
from unittest.mock import MagicMock, patch

import pytest

# Mock settings before importing modules that use them
with patch("src.config.get_settings") as mock_get_settings:
    mock_settings = MagicMock()
    mock_settings.database_url = "sqlite:///:memory:"
    mock_settings.memory_decay_days = 30
    mock_settings.memory_top_n_lessons = 5
    mock_settings.groq_api_key.get_secret_value.return_value = "fake_key"
    mock_settings.groq_model_fallback = "llama3-8b-8192"
    mock_get_settings.return_value = mock_settings

    from src.memory.analyzer import TradeOutcome, TradeOutcomeAnalyzer
    from src.memory.classifier import ClassifiedMistake, MistakeCategory, MistakeClassifier
    from src.memory.database import AgentMemoryDB, LessonRecord
    from src.memory.injection import MemoryInjector, create_feedback_loop

# --- AgentMemoryDB Tests ---


@pytest.fixture
def memory_db():
    # Use in-memory SQLite for testing
    db = AgentMemoryDB(database_url="sqlite:///:memory:")
    return db


def test_store_and_get_lesson(memory_db):
    memory_db.store_lesson(
        lesson_id="test_lesson_1",
        category="test_category",
        severity="high",
        description="Test description",
        lesson="Test lesson",
        strategy="momentum",
        regime="bull",
        symbol="AAPL",
    )

    lessons = memory_db.get_lessons(category="test_category")
    assert len(lessons) == 1
    assert lessons[0]["lesson_id"] == "test_lesson_1"
    assert lessons[0]["severity"] == "high"


def test_get_lessons_filters(memory_db):
    memory_db.store_lesson("l1", "cat1", "low", "desc", "lesson", strategy="s1", regime="r1")
    memory_db.store_lesson("l2", "cat2", "high", "desc", "lesson", strategy="s2", regime="r1")
    memory_db.store_lesson("l3", "cat1", "medium", "desc", "lesson", strategy="s1", regime="r2")

    assert len(memory_db.get_lessons(category="cat1")) == 2
    assert len(memory_db.get_lessons(strategy="s1")) == 2
    assert len(memory_db.get_lessons(regime="r1")) == 2
    assert len(memory_db.get_lessons(min_severity="high")) == 1
    assert len(memory_db.get_lessons(min_severity="medium")) == 2  # high + medium


def test_get_top_lessons_for_context(memory_db):
    # regime match
    memory_db.store_lesson("l1", "cat1", "medium", "desc", "lesson", regime="bull")
    # strategy match
    memory_db.store_lesson("l2", "cat2", "medium", "desc", "lesson", strategy="strat1")
    # high severity
    memory_db.store_lesson("l3", "cat3", "high", "desc", "lesson")
    # unrelated
    memory_db.store_lesson("l4", "cat4", "low", "desc", "lesson", regime="bear", strategy="strat2")

    lessons = memory_db.get_top_lessons_for_context(regime="bull", strategies=["strat1"], n=10)

    ids = [l["lesson_id"] for l in lessons]
    assert "l1" in ids
    assert "l2" in ids
    assert "l3" in ids
    assert "l4" not in ids


def test_mark_used(memory_db):
    memory_db.store_lesson("l1", "cat", "low", "desc", "lesson")

    memory_db.mark_used("l1", was_successful=False)
    lesson = memory_db.get_lessons()[0]
    assert lesson["use_count"] == 1
    assert lesson["success_count"] == 0

    memory_db.mark_used("l1", was_successful=True)
    lesson = memory_db.get_lessons()[0]
    assert lesson["use_count"] == 2
    assert lesson["success_count"] == 1
    # Score should increase
    assert lesson["current_score"] > 0.5  # Base score for low is 0.5


def test_delete_lesson(memory_db):
    memory_db.store_lesson("l1", "cat", "low", "desc", "lesson")
    assert memory_db.delete_lesson("l1") is True
    assert len(memory_db.get_lessons()) == 0
    assert memory_db.delete_lesson("l1") is False


def test_cleanup_expired(memory_db):
    # Expired
    memory_db.store_lesson("l1", "cat", "low", "desc", "lesson", expires_in_days=-1)
    # Valid
    memory_db.store_lesson("l2", "cat", "high", "desc", "lesson", expires_in_days=1)

    count = memory_db.cleanup_expired()
    assert count >= 1  # l1

    lessons = memory_db.get_lessons(include_expired=True)
    ids = [l["lesson_id"] for l in lessons]
    assert "l1" not in ids
    assert "l2" in ids


def test_apply_decay(memory_db):
    memory_db.store_lesson("l1", "cat", "low", "desc", "lesson")

    # Manually backdate created_at
    record = memory_db._session.query(LessonRecord).filter_by(lesson_id="l1").first()
    record.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=40
    )  # older than 30 days decay default
    memory_db._session.commit()

    memory_db.get_lessons()  # Triggers _apply_decay

    record = memory_db._session.query(LessonRecord).filter_by(lesson_id="l1").first()
    assert record.current_score < record.base_score


def test_get_memory_stats(memory_db):
    memory_db.store_lesson("l1", "cat1", "low", "desc", "lesson")
    memory_db.store_lesson("l2", "cat1", "high", "desc", "lesson")

    stats = memory_db.get_memory_stats()
    assert stats["total_lessons"] == 2
    assert stats["by_category"]["cat1"] == 2
    assert stats["by_severity"]["low"] == 1
    assert stats["by_severity"]["high"] == 1


# --- TradeOutcomeAnalyzer Tests ---


@pytest.fixture
def mock_journal():
    journal = MagicMock()
    return journal


@pytest.fixture
def analyzer(mock_journal):
    return TradeOutcomeAnalyzer(journal=mock_journal)


def test_analyze_trade_not_found(analyzer, mock_journal):
    mock_journal.get_trade.return_value = None
    assert analyzer.analyze_trade("t1") is None


def test_analyze_trade_open(analyzer, mock_journal):
    mock_journal.get_trade.return_value = {"status": "open"}
    assert analyzer.analyze_trade("t1") is None


def test_analyze_trade_winner(analyzer, mock_journal):
    mock_journal.get_trade.return_value = {
        "trade_id": "t1",
        "status": "closed",
        "entry_price": 100,
        "exit_price": 110,
        "side": "BUY",
        "profit_loss": 10,
        "profit_loss_pct": 10,
        "mae": 2,
        "mfe": 12,
        "hold_duration_minutes": 60,
        "target_price": 110,
        "stop_loss": 90,
    }

    outcome = analyzer.analyze_trade("t1")
    assert outcome.is_winner is True
    assert outcome.efficiency == 10 / 12
    assert outcome.hit_target is True
    assert outcome.hit_stop_loss is False
    assert outcome.was_premature_exit is False


def test_analyze_trade_loser(analyzer, mock_journal):
    mock_journal.get_trade.return_value = {
        "trade_id": "t2",
        "status": "closed",
        "entry_price": 100,
        "exit_price": 90,
        "side": "BUY",
        "profit_loss": -10,
        "profit_loss_pct": -10,
        "mae": 10,
        "mfe": 5,
        "hold_duration_minutes": 60,
        "target_price": 120,
        "stop_loss": 90,
    }

    outcome = analyzer.analyze_trade("t2")
    assert outcome.is_winner is False
    assert outcome.hit_stop_loss is True


def test_analyze_recent_trades(analyzer, mock_journal):
    mock_journal.get_trades_by_date.return_value = [
        {"trade_id": "t1", "status": "closed", "profit_loss": 10, "strategy": "s1"},
        {"trade_id": "t2", "status": "open"},
    ]

    outcomes = analyzer.analyze_recent_trades()
    assert len(outcomes) == 1
    assert outcomes[0].trade_id == "t1"


def test_get_strategy_performance(analyzer, mock_journal):
    mock_journal.get_trades_by_date.return_value = [
        {
            "trade_id": "t1",
            "status": "closed",
            "profit_loss": 10,
            "profit_loss_pct": 10,
            "strategy": "s1",
            "regime": "bull",
            "efficiency": 0.8,
        },
        {
            "trade_id": "t2",
            "status": "closed",
            "profit_loss": -5,
            "profit_loss_pct": -5,
            "strategy": "s1",
            "regime": "bull",
            "efficiency": 0,
        },
    ]

    perf = analyzer.get_strategy_performance("s1")
    assert perf["total_trades"] == 2
    assert perf["win_rate"] == 50.0
    assert perf["avg_profit_pct"] == 2.5


def test_identify_patterns(analyzer):
    outcomes = [
        TradeOutcome(
            "t1", "sym", "s1", "bull", False, -10, -5, 10, 0, 0, 10, False, False, True, False
        ),
        TradeOutcome(
            "t2", "sym", "s1", "bull", False, -10, -5, 10, 0, 0, 10, False, False, True, False
        ),
        TradeOutcome(
            "t3", "sym", "s1", "bull", False, -10, -5, 10, 0, 0, 10, False, False, True, False
        ),
    ]

    patterns = analyzer.identify_patterns(outcomes)
    # Should find regime_mismatch and stop_loss_issue
    types = [p["type"] for p in patterns]
    assert "regime_mismatch" in types
    assert "stop_loss_issue" in types


# --- MistakeClassifier Tests ---


@pytest.fixture
def classifier():
    with patch("src.memory.classifier.get_settings") as mock_settings:
        mock_settings.return_value.groq_api_key.get_secret_value.return_value = "fake"
        mock_settings.return_value.groq_model_fallback = "fake-model"
        with patch("src.memory.classifier.ChatGroq") as mock_llm:
            clf = MistakeClassifier()
            clf._llm = mock_llm
            yield clf


def test_should_classify(classifier):
    # Loser
    o1 = TradeOutcome("t1", "s", "st", "r", False, -10, -1, 0, 0, 0, 10, False, False, False, False)
    assert classifier._should_classify(o1) is True

    # Efficient Winner
    o2 = TradeOutcome("t2", "s", "st", "r", True, 10, 1, 0, 20, 0.8, 10, False, False, False, False)
    assert classifier._should_classify(o2) is False

    # Inefficient Winner
    o3 = TradeOutcome("t3", "s", "st", "r", True, 10, 1, 0, 30, 0.3, 10, False, False, False, False)
    assert classifier._should_classify(o3) is True


def test_rule_based_classify(classifier):
    # Stop loss tight
    o = TradeOutcome("t1", "s", "st", "r", False, -10, -1, 0, 0, 0, 5, False, False, True, False)
    res = classifier._rule_based_classify(o)
    assert res["category"] == MistakeCategory.STOP_LOSS_TOO_TIGHT
    assert res["severity"] == "high"


def test_llm_classify(classifier):
    mock_response = MagicMock()
    mock_response.content = '{"category": "strategy_mismatch", "severity": "medium", "description": "desc", "lesson": "lesson"}'
    classifier._llm.invoke.return_value = mock_response

    o = TradeOutcome("t1", "s", "st", "r", False, -10, -1, 0, 0, 0, 10, False, False, False, False)
    res = classifier._llm_classify(o)
    assert res["category"] == "strategy_mismatch"


def test_classify_integration(classifier):
    mock_response = MagicMock()
    mock_response.content = '{"category": "strategy_mismatch", "severity": "medium", "description": "desc", "lesson": "lesson"}'
    classifier._llm.invoke.return_value = mock_response

    o = TradeOutcome("t1", "s", "st", "r", False, -10, -1, 0, 0, 0, 10, False, False, False, False)

    result = classifier.classify(o)
    assert isinstance(result, ClassifiedMistake)
    assert result.trade_id == "t1"
    assert result.category == "strategy_mismatch"


# --- MemoryInjector Tests ---


@pytest.fixture
def injector(memory_db):
    return MemoryInjector(memory_db=memory_db)


def test_inject_lessons(injector, memory_db):
    memory_db.store_lesson("l1", "cat", "high", "desc", "lesson", regime="bull")

    state = {"regime": "bull", "active_strategies": []}
    result = injector.inject_lessons(state)

    assert "memory_lessons" in result
    assert len(result["memory_lessons"]) == 1
    assert result["memory_lessons"][0]["lesson_id"] == "l1"


def test_store_from_classifier(injector, memory_db):
    mistake = ClassifiedMistake(
        "lid",
        "tid",
        "cat",
        "sev",
        "desc",
        "lesson",
        [],
        {"strategy": "s", "regime": "r", "symbol": "sym"},
        datetime.datetime.now(),
    )

    injector.store_from_classifier(mistake)
    assert len(memory_db.get_lessons()) == 1


def test_format_lessons_for_agent(injector):
    lessons = [
        {
            "severity": "high",
            "category": "cat1",
            "description": "d1",
            "lesson": "l1",
            "strategy": "s1",
            "regime": "r1",
        },
        {"severity": "medium", "category": "cat2", "description": "d2", "lesson": "l2"},
    ]

    formatted = injector.format_lessons_for_agent(lessons)
    assert "Lesson 1: [HIGH] Cat1" in formatted
    assert "Lesson 2: [MEDIUM] Cat2" in formatted
    assert "d1" in formatted
    assert "l1" in formatted


def test_create_feedback_loop(memory_db):
    classifier = MagicMock()
    classifier.classify.return_value = ClassifiedMistake(
        "lid", "tid", "cat", "sev", "desc", "lesson", [], {}, datetime.datetime.now()
    )

    outcomes = [MagicMock(trade_id="t1")]

    count = create_feedback_loop(outcomes, classifier, memory_db)
    assert count == 1
    assert len(memory_db.get_lessons()) == 1
