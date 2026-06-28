"""
LangSmith Tracing Module

Provides observability for agent decisions via LangSmith integration.
Includes decorators, metadata tagging, and trace management.
"""

import functools
import logging
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from langsmith import Client, traceable
from langsmith.run_helpers import get_current_run_tree

from src.config import get_settings

logger = logging.getLogger(__name__)


def setup_tracing() -> bool:
    """
    Setup LangSmith tracing for the trading system.

    Configures environment variables and validates connection.

    Returns:
        True if tracing is enabled and working
    """
    try:
        settings = get_settings()

        # Set environment variables for LangSmith
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_TRACING_V2"] = str(settings.langsmith_tracing_v2).lower()

        # Validate connection
        client = Client()

        # Try to list projects to verify connection
        list(client.list_projects(limit=1))

        logger.info(f"LangSmith tracing enabled for project: {settings.langsmith_project}")
        return True

    except Exception as e:
        logger.warning(f"LangSmith tracing not available: {e}")
        return False


def trace_agent(
    agent_name: str,
    run_type: str = "chain",
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """
    Decorator for tracing agent functions.

    Args:
        agent_name: Name of the agent (e.g., "market_regime", "strategy_selection")
        run_type: Type of run (chain, llm, tool, etc.)
        metadata: Additional metadata to attach to the trace

    Returns:
        Decorated function with tracing
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()

            # Build trace metadata
            trace_metadata = {
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
            }
            if metadata:
                trace_metadata.update(metadata)

            try:
                # Execute with tracing
                result = func(*args, **kwargs)

                # Calculate latency
                latency_ms = int((time.time() - start_time) * 1000)

                # Log success
                logger.debug(f"Agent {agent_name} completed in {latency_ms}ms")

                return result

            except Exception as e:
                logger.error(f"Agent {agent_name} failed: {e}")
                raise

        # Apply LangSmith traceable decorator
        return traceable(name=agent_name, run_type=run_type, metadata=metadata or {})(wrapper)

    return decorator


@contextmanager
def trading_trace(
    workflow_id: str,
    regime: str | None = None,
    strategies: list[str] | None = None,
    signals_count: int = 0,
):
    """
    Context manager for tracing a complete trading cycle.

    Creates a parent trace with full context for the trading workflow.

    Usage:
        with trading_trace("WF-123", regime="trending_up") as trace:
            # Trading operations
            pass
    """
    metadata = {
        "workflow_id": workflow_id,
        "regime": regime,
        "strategies": strategies or [],
        "signals_count": signals_count,
        "started_at": datetime.now().isoformat(),
    }

    start_time = time.time()

    try:
        yield metadata

        metadata["status"] = "success"
        metadata["duration_ms"] = int((time.time() - start_time) * 1000)

    except Exception as e:
        metadata["status"] = "error"
        metadata["error"] = str(e)
        metadata["duration_ms"] = int((time.time() - start_time) * 1000)
        raise

    finally:
        logger.info(
            f"Trading cycle {workflow_id} completed: "
            f"status={metadata.get('status')}, duration={metadata.get('duration_ms')}ms"
        )


def add_trace_metadata(
    key: str,
    value: Any,
) -> None:
    """
    Add metadata to the current trace.

    Args:
        key: Metadata key
        value: Metadata value
    """
    try:
        run_tree = get_current_run_tree()
        if run_tree:
            if run_tree.extra is None:
                run_tree.extra = {}
            if "metadata" not in run_tree.extra:
                run_tree.extra["metadata"] = {}
            run_tree.extra["metadata"][key] = value
    except Exception as e:
        logger.debug(f"Could not add trace metadata: {e}")


def tag_trace(
    trade_id: str | None = None,
    decision: str | None = None,
    signal_id: str | None = None,
) -> None:
    """
    Tag the current trace with trading-specific identifiers.

    Args:
        trade_id: Trade identifier
        decision: Decision made (approve/reject/hold)
        signal_id: Signal identifier
    """
    if trade_id:
        add_trace_metadata("trade_id", trade_id)
    if decision:
        add_trace_metadata("decision", decision)
    if signal_id:
        add_trace_metadata("signal_id", signal_id)


class TracingCallback:
    """
    Callback handler for custom tracing events.

    Can be used to capture additional data during agent execution.
    """

    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        self.events = []

    def on_agent_start(self, agent_name: str, input_data: dict[str, Any]) -> None:
        """Called when an agent starts execution."""
        self.events.append(
            {
                "type": "agent_start",
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "workflow_id": self.workflow_id,
            }
        )
        logger.debug(f"Agent {agent_name} started")

    def on_agent_end(
        self,
        agent_name: str,
        output_data: dict[str, Any],
        latency_ms: int,
    ) -> None:
        """Called when an agent completes execution."""
        self.events.append(
            {
                "type": "agent_end",
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "latency_ms": latency_ms,
                "workflow_id": self.workflow_id,
            }
        )
        logger.debug(f"Agent {agent_name} completed in {latency_ms}ms")

    def on_decision(
        self,
        agent_name: str,
        decision: str,
        confidence: float,
        reasoning: str,
    ) -> None:
        """Called when an agent makes a decision."""
        self.events.append(
            {
                "type": "decision",
                "agent": agent_name,
                "decision": decision,
                "confidence": confidence,
                "reasoning": reasoning[:200],  # Truncate long reasoning
                "timestamp": datetime.now().isoformat(),
                "workflow_id": self.workflow_id,
            }
        )
        add_trace_metadata(f"{agent_name}_decision", decision)
        add_trace_metadata(f"{agent_name}_confidence", confidence)

    def on_error(self, agent_name: str, error: str) -> None:
        """Called when an agent encounters an error."""
        self.events.append(
            {
                "type": "error",
                "agent": agent_name,
                "error": error,
                "timestamp": datetime.now().isoformat(),
                "workflow_id": self.workflow_id,
            }
        )
        logger.error(f"Agent {agent_name} error: {error}")

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all traced events."""
        return {
            "workflow_id": self.workflow_id,
            "event_count": len(self.events),
            "events": self.events,
            "agents_run": list(set(e["agent"] for e in self.events if "agent" in e)),
        }


def create_tracing_config(
    workflow_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create configuration dict for LangGraph with tracing.

    Args:
        workflow_id: Unique workflow identifier
        metadata: Additional metadata

    Returns:
        Config dict for graph.invoke()
    """
    config = {
        "configurable": {
            "thread_id": workflow_id,
        },
        "metadata": {
            "workflow_id": workflow_id,
            "workflow_type": "trading_cycle",
            "started_at": datetime.now().isoformat(),
        },
        "callbacks": [],
    }

    if metadata:
        config["metadata"].update(metadata)

    return config
