"""
RakshaQuant — main entry point.

Runs the one shared trading loop (``src.live.session.run_trading_session``) in one of two
front ends, selected with ``--mode``:

* ``cli``  (default) — the ``rich`` terminal dashboard. Unchanged from before.
* ``web``           — a FastAPI + WebSocket server driving the browser console.

Both modes drive identical trading logic; the web layer is a presentation/control shell.

Examples::

    uv run python scripts/run_live_trading.py                 # CLI (default)
    uv run python scripts/run_live_trading.py --mode web       # web console
    uv run python scripts/run_live_trading.py --mode web --demo # web console, synthetic data
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from src.dashboard.cli import TradingStats
from src.live.session import run_trading_session
from src.live.views import RichSessionView

console = Console()
logger = logging.getLogger(__name__)


async def _run_cli() -> None:
    """CLI mode: drive the shared session through the rich terminal dashboard."""
    stats = TradingStats()
    view = RichSessionView(stats)
    await run_trading_session(view)


def _run_web(args: argparse.Namespace) -> None:
    """Web mode: launch the FastAPI console (requires the ``web`` extra)."""
    try:
        from src.web.server import run_web
    except ImportError as exc:  # pragma: no cover - missing optional deps
        console.print(
            "[red]Web mode requires the 'web' extra.[/] Install it with:\n"
            "  [cyan]uv sync --extra web[/]  (or  pip install '.[web]')\n"
            f"[dim]{exc}[/]"
        )
        sys.exit(1)

    run_web(
        host=args.host,
        port=args.port,
        demo=args.demo,
        dev=args.dev,
        auto_start=not args.no_auto_start,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RakshaQuant — agentic NSE paper-trading (CLI or web)."
    )
    parser.add_argument(
        "--mode",
        choices=["cli", "web"],
        default="cli",
        help="Front end to run (default: cli).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Web mode: bind host.")
    parser.add_argument("--port", type=int, default=8000, help="Web mode: bind port.")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Web mode: run a synthetic demo session (no market data / API keys).",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Web mode: enable CORS for the Vite dev server (localhost:5173).",
    )
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Web mode: start the server without auto-starting a trading run.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Main entry point."""
    import atexit
    import warnings

    args = _parse_args()

    def suppress_threading_errors() -> None:
        warnings.filterwarnings("ignore", category=RuntimeWarning)

    atexit.register(suppress_threading_errors)

    if args.mode == "web":
        _run_web(args)
        return

    try:
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")
        raise
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
