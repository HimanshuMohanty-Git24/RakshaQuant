"""
Session views - the renderers (CLI dashboard, web stream) behind the trading engine.

Since plan M5.6 they render the v2 engine (:mod:`src.engine.live`), fed from the event store's
projections. The legacy ``run_trading_session`` loop was retired to :mod:`src.legacy.session` (M5.5).
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
