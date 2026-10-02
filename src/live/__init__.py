"""
Session views - the renderers (CLI dashboard, web stream) behind the trading engine.

They render the v2 engine (:mod:`src.engine.live`), fed from the event store's projections.
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
