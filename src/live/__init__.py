"""
Session views - the renderers (CLI dashboard, web stream) behind the trading engine.

Since plan M5.6 they render the v2 engine (:mod:`src.engine.live`), fed from the event store's
projections. The legacy ``run_trading_session`` loop is no longer on any live path; import it from
:mod:`src.live.session` only where the legacy tests still need it (moved to ``src/legacy`` in 5.5).
"""

from src.live.recorder import CycleRecorder, CycleTrace, Span, snapshot_from_stats
from src.live.views import RichSessionView, SessionView, StreamSessionView

__all__ = [
    "SessionView",
    "RichSessionView",
    "StreamSessionView",
    "CycleRecorder",
    "CycleTrace",
    "Span",
    "snapshot_from_stats",
]
