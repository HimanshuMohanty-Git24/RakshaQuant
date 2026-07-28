"""
Session views — the two renderers behind the one shared trading loop.

* :class:`RichSessionView` drives the existing ``rich`` terminal dashboard. It reproduces
  the CLI's exact behaviour (alternate-screen ``Live``, per-second redraws during waits),
  so ``--mode cli`` is unchanged.
* :class:`StreamSessionView` serialises the same :class:`TradingStats` into JSON snapshots
  and pushes them (plus completed cycle traces) to a sink for the web UI. Its waits are
  non-blocking (``await asyncio.sleep``) so the server's event loop keeps serving sockets.

The loop in :mod:`src.live.session` only ever talks to this interface, never to ``rich`` or
to a socket directly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.live.recorder import CycleRecorder, CycleTrace, snapshot_from_stats

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.live import Live

    from src.dashboard.cli import TradingStats

logger = logging.getLogger(__name__)

# Strips rich console markup (e.g. "[bold green]", "[/]", "[dim]") from banner strings so it
# never reaches the browser feed as raw text. Matches an opening/closing tag or a bare "[/]".
_RICH_MARKUP = re.compile(r"\[/?[a-zA-Z#][^\]]*\]|\[/\]")


@runtime_checkable
class SnapshotSink(Protocol):
    """Where a :class:`StreamSessionView` publishes state (implemented by the web RunManager)."""

    def set_snapshot(self, snapshot: dict[str, Any]) -> None: ...
    def add_cycle(self, cycle: dict[str, Any]) -> None: ...


class SessionView:
    """Interface the trading loop renders through. Async context manager."""

    stats: TradingStats
    recorder: CycleRecorder | None = None
    run_status: str = "IDLE"

    async def open(self) -> None:  # pragma: no cover - interface
        ...

    async def close(self) -> None:  # pragma: no cover - interface
        ...

    async def render(self) -> None:  # pragma: no cover - interface
        """Push the current stats to the output."""

    async def wait(self, seconds: int) -> None:  # pragma: no cover - interface
        """Keep-alive wait that keeps the output responsive."""

    def note(self, message: str) -> None:  # pragma: no cover - interface
        """A one-off status line (startup/shutdown banners)."""

    def set_effective_mode(self, mode: str) -> None:  # pragma: no cover - interface
        """Receive the resolved execution mode once the session computes it (for the badge)."""

    async def emit_cycle(self, trace: CycleTrace) -> None:  # pragma: no cover - interface
        """Publish a completed trade-cycle trace (no-op for the CLI)."""

    async def __aenter__(self) -> SessionView:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class RichSessionView(SessionView):
    """Renders to the ``rich`` terminal dashboard — identical to the legacy CLI behaviour."""

    def __init__(self, stats: TradingStats) -> None:
        # Imported lazily so the web mode never needs ``rich`` wired to a real TTY.
        from rich.console import Console

        from src.dashboard.cli import create_dashboard_layout

        self.stats = stats
        self.recorder = None  # CLI does not build traces (keeps behaviour + cost identical).
        self.console = Console()
        self._layout = create_dashboard_layout
        self._live: Live | None = None

    async def open(self) -> None:
        from rich.live import Live

        self.run_status = "RUNNING"
        # screen=True → stable full-screen render on the alternate buffer (no scroll/flicker).
        self._live = Live(
            self._layout(self.stats),
            console=self.console,
            refresh_per_second=4,
            screen=True,
        )
        self._live.__enter__()

    async def close(self) -> None:
        self.run_status = "DONE"
        if self._live is not None:
            self._live.__exit__(None, None, None)
            self._live = None

    async def render(self) -> None:
        if self._live is not None:
            self._live.update(self._layout(self.stats))

    async def wait(self, seconds: int) -> None:
        # Blocking per-second redraw, matching the legacy loop exactly.
        for _ in range(max(0, int(seconds))):
            time.sleep(1)
            await self.render()

    def note(self, message: str) -> None:
        self.console.print(message)

    async def emit_cycle(self, trace: CycleTrace) -> None:
        return None


class StreamSessionView(SessionView):
    """Serialises the same state to JSON and streams it to the web UI via a sink."""

    def __init__(
        self,
        stats: TradingStats,
        sink: SnapshotSink,
        *,
        effective_mode: str,
        refresh_seconds: float = 1.0,
    ) -> None:
        self.stats = stats
        self.sink = sink
        self.effective_mode = effective_mode
        self.refresh_seconds = refresh_seconds
        self.recorder = CycleRecorder()
        self.run_status = "IDLE"

    async def open(self) -> None:
        self.run_status = "RUNNING"
        await self.render()

    async def close(self) -> None:
        self.run_status = "DONE"
        await self.render()

    async def render(self) -> None:
        try:
            snapshot = snapshot_from_stats(
                self.stats,
                run_status=self.run_status,
                effective_mode=self.effective_mode,
            )
            self.sink.set_snapshot(snapshot)
        except Exception as exc:  # pragma: no cover - serialisation must never kill the loop
            logger.warning("Snapshot serialisation failed (non-fatal): %s", exc)

    async def wait(self, seconds: int) -> None:
        # Non-blocking: render once, then yield the event loop so the server keeps serving.
        await self.render()
        await asyncio.sleep(max(0, int(seconds)))

    def note(self, message: str) -> None:
        # Surface banner lines in the live activity feed. Strip rich console markup first so
        # tags like "[bold green]...[/]" don't render as raw text in the browser.
        try:
            self.stats.log_activity(_RICH_MARKUP.sub("", message).strip(), "INFO")
        except Exception:  # pragma: no cover - defensive
            pass

    def set_effective_mode(self, mode: str) -> None:
        self.effective_mode = mode

    async def emit_cycle(self, trace: CycleTrace) -> None:
        try:
            self.sink.add_cycle(trace.to_dict())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Cycle publish failed (non-fatal): %s", exc)
