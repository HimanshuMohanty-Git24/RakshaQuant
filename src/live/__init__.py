"""
Live trading session — the shared driver behind both the CLI and web front ends.

``run_trading_session`` is the single source of trading logic. It is parameterised by a
:class:`~src.live.views.SessionView` so the exact same loop can render to a ``rich``
terminal dashboard (``RichSessionView``) or stream JSON snapshots to the web UI
(``StreamSessionView``). This keeps one trading path — there is deliberately no second,
divergent loop for the browser.
"""

from src.live.recorder import CycleRecorder, CycleTrace, Span, snapshot_from_stats
from src.live.session import run_trading_session
from src.live.views import RichSessionView, SessionView, StreamSessionView

__all__ = [
    "run_trading_session",
    "SessionView",
    "RichSessionView",
    "StreamSessionView",
    "CycleRecorder",
    "CycleTrace",
    "Span",
    "snapshot_from_stats",
]
