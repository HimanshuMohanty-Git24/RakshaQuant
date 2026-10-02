"""
RakshaQuant — main entry point.

Runs the v2 trading engine (``src.engine.live``: one paper book on the simulated broker,
YFinance data, the RiskEngine on every order) behind one of two front ends, selected with
``--mode``:

* ``cli``  (default) — the ``rich`` terminal dashboard.
* ``web``           — a FastAPI + WebSocket server driving the browser console.

Both modes drive the same engine; the views are fed from the event store's projections.
``--demo`` replays the bundled fixture tape (one synthetic session) through the same engine in
the ``demo`` environment.

Examples::

    uv run python scripts/run_live_trading.py                 # CLI (default)
    uv run python scripts/run_live_trading.py --mode web       # web console
    uv run python scripts/run_live_trading.py --mode web --demo # web console, the demo tape
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from src.config.settings import get_settings
from src.dashboard.cli import TradingStats
from src.engine.live import run_demo, run_paper
from src.live.views import RichSessionView
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point

console = Console()
logger = logging.getLogger(__name__)


async def _run_cli(demo: bool) -> int:
    """CLI mode: the engine behind the rich terminal dashboard."""
    view = RichSessionView(TradingStats())
    settings = get_settings()
    return await (run_demo(settings, view) if demo else run_paper(settings, view))


def _run_web(args: argparse.Namespace) -> int:
    """Web mode: launch the FastAPI console (requires the ``web`` extra)."""
    try:
        from src.web.server import run_web
    except ImportError as exc:  # pragma: no cover - missing optional deps
        console.print(
            "[red]Web mode requires the 'web' extra.[/] Install it with:\n"
            "  [cyan]uv sync --extra web[/]  (or  pip install '.[web]')\n"
            f"[dim]{exc}[/]"
        )
        return ExitCode.CONFIG_ERROR

    run_web(
        host=args.host,
        port=args.port,
        demo=args.demo,
        dev=args.dev,
        auto_start=not args.no_auto_start,
        allow_remote=args.allow_remote,
    )
    return ExitCode.OK


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
        help="Demo: simulated data in the separate 'demo' environment (own state, wallet and "
        "lock). The only way simulated prices may create orders.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Web mode: enable CORS for the Vite dev server (localhost:5173).",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Web mode: allow binding --host to a non-loopback address (reachable from other "
        "machines; the access token is still required).",
    )
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Web mode: start the server without auto-starting a trading run.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Main entry point.

    Exit codes: 0 normal (incl. Ctrl-C), 1 crash, 2 configuration error, 3 another instance
    holds this environment's state directory. Logs go to ``var/logs/``; in CLI mode nothing is
    logged to the console, which belongs to the dashboard.
    """
    args = _parse_args()
    if args.demo:
        # Plan M2.6: simulated data only ever runs in the demo environment (var/demo/).
        os.environ["ENVIRONMENT"] = "demo"
    run_entry_point(
        "run_live_trading",
        lambda: _run(args),
        lock=True,
        record_events=True,
        console_log_level="INFO" if args.mode == "web" else None,
    )


def _run(args: argparse.Namespace) -> int:
    import atexit
    import warnings

    def suppress_threading_errors() -> None:
        warnings.filterwarnings("ignore", category=RuntimeWarning)

    atexit.register(suppress_threading_errors)

    if args.mode == "web":
        return _run_web(args)

    return asyncio.run(_run_cli(args.demo))


if __name__ == "__main__":
    main()
