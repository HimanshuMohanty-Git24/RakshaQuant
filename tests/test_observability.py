import os
from unittest.mock import MagicMock, patch

import pytest

from src.observability.tracing import (
    TracingCallback,
    add_trace_metadata,
    create_tracing_config,
    setup_tracing,
    tag_trace,
    trace_agent,
    trading_trace,
)


@pytest.fixture
def mock_settings():
    with patch("src.observability.tracing.get_settings") as mock:
        mock.return_value.langsmith_api_key.get_secret_value.return_value = "token"
        mock.return_value.langsmith_project = "test-project"
        mock.return_value.langsmith_tracing_v2 = "true"
        yield mock


def test_setup_tracing(mock_settings):
    with patch("src.observability.tracing.Client") as mock_client:
        mock_client.return_value.list_projects.return_value = ["p1"]

        success = setup_tracing()

        assert success is True
        assert os.environ["LANGSMITH_PROJECT"] == "test-project"


def test_setup_tracing_fail(mock_settings):
    with patch("src.observability.tracing.Client") as mock_client:
        mock_client.side_effect = Exception("Connect fail")

        success = setup_tracing()
        assert success is False


def test_trace_agent_decorator():
    # Mock traceable to just return the function wrapper
    with patch("src.observability.tracing.traceable") as mock_traceable:
        mock_traceable.return_value = lambda f: f

        @trace_agent("test_agent")
        def my_func(x):
            return x * 2

        result = my_func(2)
        assert result == 4


def test_trading_trace_context():
    with trading_trace("wf1", regime="bull") as meta:
        assert meta["workflow_id"] == "wf1"
        assert meta["regime"] == "bull"

    assert meta["status"] == "success"
    assert "duration_ms" in meta


def test_trading_trace_error():
    with pytest.raises(ValueError):
        with trading_trace("wf1") as meta:
            raise ValueError("Test error")

    assert meta["status"] == "error"
    assert "Test error" in meta["error"]


def test_add_trace_metadata():
    with patch("src.observability.tracing.get_current_run_tree") as mock_get_tree:
        mock_tree = MagicMock()
        mock_tree.extra = {}
        mock_get_tree.return_value = mock_tree

        add_trace_metadata("key", "value")

        assert mock_tree.extra["metadata"]["key"] == "value"


def test_tag_trace():
    with patch("src.observability.tracing.add_trace_metadata") as mock_add:
        tag_trace(trade_id="t1", decision="buy", signal_id="s1")

        mock_add.assert_any_call("trade_id", "t1")
        mock_add.assert_any_call("decision", "buy")
        mock_add.assert_any_call("signal_id", "s1")


def test_tracing_callback():
    cb = TracingCallback("wf1")

    cb.on_agent_start("agent1", {})
    assert len(cb.events) == 1
    assert cb.events[0]["type"] == "agent_start"

    cb.on_agent_end("agent1", {}, 100)
    assert len(cb.events) == 2
    assert cb.events[1]["type"] == "agent_end"

    cb.on_decision("agent1", "buy", 0.9, "reason")
    assert len(cb.events) == 3
    assert cb.events[2]["type"] == "decision"

    cb.on_error("agent1", "err")
    assert len(cb.events) == 4
    assert cb.events[3]["type"] == "error"

    summary = cb.get_summary()
    assert summary["event_count"] == 4
    assert "agent1" in summary["agents_run"]


def test_create_tracing_config():
    config = create_tracing_config("wf1", metadata={"key": "val"})

    assert config["configurable"]["thread_id"] == "wf1"
    assert config["metadata"]["workflow_id"] == "wf1"
    assert config["metadata"]["key"] == "val"
    assert "started_at" in config["metadata"]
