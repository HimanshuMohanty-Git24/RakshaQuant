"""
Regression tests for owner-reported runtime issues #17, #18, #19.

- #18: numpy scalars reaching the LangGraph msgpack checkpoint boundary crash the cycle
  (``TypeError: Type is not msgpack serializable: numpy.float64``). ``to_native`` coerces
  them to native Python types; ``PredictionSignal.to_dict`` casts at source.
- #17: ``None`` indicator values formatted with a numeric spec crash the cycle
  (``TypeError: unsupported format string passed to NoneType.__format__``). ``fmt_optional``
  renders a placeholder instead.
- #19: outside NSE trading hours the deterministic risk engine blocks *every* entry — this
  is by design, and the block carries a human-readable reason.
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import patch

# The Settings model requires these; keep the file self-contained (LLM is never called here).
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("LANGSMITH_API_KEY", "test-key")

import numpy as np
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from src.agents.prediction import PredictionSignal
from src.agents.risk_compliance import risk_compliance_node
from src.utils.formatting import fmt_optional
from src.utils.market_time import IST
from src.utils.serialization import to_native

# ---------------------------------------------------------------------------
# #18 — to_native converts numpy scalars/arrays to native Python types
# ---------------------------------------------------------------------------


def test_to_native_converts_numpy_scalars() -> None:
    out_f = to_native(np.float64(1.5))
    out_i = to_native(np.int64(7))
    out_b = to_native(np.bool_(True))

    assert out_f == 1.5 and type(out_f) is float
    assert out_i == 7 and type(out_i) is int
    assert out_b is True and type(out_b) is bool


def test_to_native_converts_arrays_and_nested_structures() -> None:
    payload = {
        "arr": np.array([1.0, 2.0, 3.0]),
        "nested": {"conf": np.float64(0.9), "items": [np.int64(1), np.int64(2)]},
        "tuple": (np.float64(0.1), "keep"),
    }

    out = to_native(payload)

    assert out["arr"] == [1.0, 2.0, 3.0]
    assert all(type(x) is float for x in out["arr"])
    assert type(out["nested"]["conf"]) is float
    assert [type(x) for x in out["nested"]["items"]] == [int, int]
    assert out["tuple"] == [0.1, "keep"]  # tuples normalize to lists


def test_to_native_is_noop_on_native_values() -> None:
    payload = {"a": 1, "b": 2.0, "c": "x", "d": None, "e": [True, False]}
    assert to_native(payload) == payload


# ---------------------------------------------------------------------------
# #18 — the fix makes state msgpack-serializable (faithful to MemorySaver)
# ---------------------------------------------------------------------------


def _numpy_state() -> dict:
    """A state fragment shaped like the real pipeline, seeded with numpy scalars."""
    return {
        "prediction_signals": [
            {"symbol": "INFY", "confidence": np.float64(0.72), "predicted_change_pct": np.float64(1.3)}
        ],
        "daily_stats": {"profit_loss": np.float64(-250.0), "max_drawdown": np.float64(1200.0)},
        "portfolio": {"capital": np.float64(998750.0)},
    }


def test_raw_numpy_state_is_not_msgpack_serializable() -> None:
    """Documents the bug: the raw numpy state crashes the checkpoint serializer."""
    serde = JsonPlusSerializer()
    with pytest.raises(TypeError, match="numpy"):
        serde.dumps_typed(_numpy_state())


def test_to_native_state_is_msgpack_serializable() -> None:
    """The fix: after to_native the same state serializes cleanly."""
    serde = JsonPlusSerializer()
    serde.dumps_typed(to_native(_numpy_state()))  # must not raise


def test_prediction_signal_to_dict_is_native() -> None:
    """PredictionSignal.to_dict casts numpy floats at source (the primary #18 leak)."""
    sig = PredictionSignal(
        symbol="INFY",
        direction="up",
        confidence=np.float64(0.72),
        predicted_change_pct=np.float64(1.3),
        reasoning="ensemble",
    )
    d = sig.to_dict()

    assert type(d["confidence"]) is float
    assert type(d["predicted_change_pct"]) is float
    JsonPlusSerializer().dumps_typed(d)  # msgpack-safe


# ---------------------------------------------------------------------------
# #17 — fmt_optional renders a placeholder instead of raising on None
# ---------------------------------------------------------------------------


def test_fmt_optional_handles_none() -> None:
    assert fmt_optional(None, ".1f") == "N/A"
    assert fmt_optional(None, ".1f", default="n/a") == "n/a"


def test_fmt_optional_formats_numbers() -> None:
    assert fmt_optional(42.1234, ".1f") == "42.1"
    assert fmt_optional(7, "d") == "7"


def test_fmt_optional_does_not_raise_on_bad_value() -> None:
    # A numeric spec on a str would normally raise; fmt_optional swallows it.
    assert fmt_optional("not-a-number", ".2f") == "N/A"


def test_fmt_optional_matches_the_indicator_warmup_case() -> None:
    """Reproduces #17: a None RSI/ADX formatted with a numeric spec must not raise."""
    rsi = None  # indicator still in warm-up
    # Bare formatting would raise TypeError here; the helper must not.
    assert f"(RSI: {fmt_optional(rsi, '.1f')})" == "(RSI: N/A)"


# ---------------------------------------------------------------------------
# #19 — outside trading hours the risk engine blocks every entry (by design)
# ---------------------------------------------------------------------------


def test_risk_engine_blocks_entries_outside_trading_hours() -> None:
    signal = {
        "signal_id": "SIG-1",
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "position_size_pct": 5.0,
        "risk_reward_ratio": 2.0,
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "confidence": 0.7,
        "validation": {"confidence": 0.7},
    }
    state = {
        "validated_signals": [signal],
        "portfolio": {"capital": 1_000_000.0, "positions": []},
        "daily_stats": {"trades_count": 0, "profit_loss": 0.0, "max_drawdown": 0.0},
    }

    # 20:00 IST — NSE closed (the owner's off-hours / SIMULATED scenario).
    after_hours = datetime(2026, 7, 27, 20, 0, tzinfo=IST)
    with patch("src.agents.risk_compliance.now_ist", return_value=after_hours):
        result = risk_compliance_node(state)

    assert result["approved_trades"] == []
    assert len(result["risk_rejected"]) == 1

    failures = result["risk_rejected"][0]["risk_result"]["failures"]
    messages = " ".join(f["message"] for f in failures).lower()
    assert "trading hours" in messages  # the human-readable reason surfaced to the dashboard
