"""
The terminal dashboard (plan M12.2): the running engine in ``rich``, read from the same
projections as the web console (:mod:`src.web.queries`, which never imports FastAPI) - every
book's summary, the risk strip, open positions, the last decisions and warnings.

:class:`TerminalView` is the :class:`~src.engine.live.EngineView` the CLI hands to
``run_paper``/``run_demo``: painted every second on the alternate screen, and printed once more
on the normal screen when the session ends, so its final state stays visible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from src.domain.events import Disposition
from src.store.event_store import EventStore
from src.utils.market_time import IST
from src.web.models import AlertRow, BookRisk, DecisionRow, PositionRow, RiskView, Summary
from src.web.queries import LiveView, Queries, build_queries, live_view

if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.engine.runner import Engine

DECISIONS_SHOWN = 10
ALERTS_SHOWN = 4
WARN_AT = 0.8  # a limit this used turns amber; at 1.0, red
DETAIL_WIDTH = 64


@dataclass(frozen=True)
class Frame:
    """Everything one paint shows: read in a worker thread, rendered on the event loop."""

    summary: Summary
    risk: RiskView
    positions: list[PositionRow]
    decisions: list[DecisionRow]
    alerts: list[AlertRow]
    quote_age_s: Mapping[str, float]
    tasks: tuple[str, ...]
    halt_file: str


def read_frame(q: Queries, live: LiveView | None, *, halt_file: str) -> Frame:
    """One consistent read of the projections (synchronous: run it off the event loop)."""
    today = q.today()
    warnings = [a for a in q.alerts(level=None, day=today, limit=50)
                if a.level in ("WARNING", "CRITICAL")]  # fmt: skip
    return Frame(
        summary=q.summary(live, running=bool(live and live.tasks)),
        risk=q.risk(book=None, live=live),
        positions=q.positions(live, book=None),
        decisions=q.decisions(
            book=None, symbol=None, strategy=None, outcome=None, day=today, limit=DECISIONS_SHOWN
        ),
        alerts=warnings[:ALERTS_SHOWN],
        quote_age_s=dict(live.quote_age_s) if live else {},
        tasks=live.tasks if live else (),
        halt_file=halt_file,
    )


# -- rendering (pure: a Frame in, a renderable out) ----------------------------------------------


def money(value: Decimal | None, *, signed: bool = False) -> Text:
    if value is None:
        return Text("-", style="dim")
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    style = "" if not signed or value == 0 else ("green" if value > 0 else "red")
    return Text(text, style=style)


def percent(value: float | None) -> Text:
    if value is None:
        return Text("-", style="dim")
    return Text(f"{value:+.2f}%", style="" if value == 0 else ("green" if value > 0 else "red"))


def clock_ist(ts: datetime) -> str:
    return ts.astimezone(IST).strftime("%H:%M:%S")


def switch_style(state: str) -> str:
    return "green" if state == "ARMED" else "bold red"


def header(frame: Frame) -> Text:
    s = frame.summary
    out = Text()
    out.append(" RakshaQuant ", style="bold black on cyan")
    out.append("  ")
    out.append(" DEMO " if s.demo else " PAPER ", style="bold black on yellow" if s.demo
               else "bold black on green")  # fmt: skip
    out.append(f"  {s.experiment}  env {s.environment}  ")
    if s.session is not None:
        out.append(f"session {s.session.date} ")
        out.append(s.session.state, style="bold")
    else:
        out.append("no session yet", style="dim")
    out.append(f"  {clock_ist(s.now)} IST  ")
    out.append("market open" if s.market_open else "market closed",
               style="green" if s.market_open else "dim")  # fmt: skip
    upcoming = [step for step in s.schedule if step.at > s.now]
    if upcoming:
        out.append(f"  next {upcoming[0].state} {clock_ist(upcoming[0].at)}", style="dim")
    out.append(f"  seq {s.last_seq}", style="dim")
    out.append("  running" if s.running else "  stopped", style="cyan" if s.running else "dim")
    return out


def books_table(summary: Summary) -> Table:
    table = Table(title="Books", title_justify="left", box=box.SIMPLE_HEAD, expand=True)
    for name in ("Book", "Advisor", "Equity", "Day P&L", "Day %", "Realized", "Unrealized"):
        table.add_column(name, justify="left" if name in ("Book", "Advisor") else "right")
    for name in ("Pos", "Orders", "Trades"):
        table.add_column(name, justify="right")
    table.add_column("Kill switch")
    table.add_column("Valued", style="dim")
    for b in summary.books:
        table.add_row(
            Text(b.book_id, style="bold"), b.advisor, money(b.equity),
            money(b.day_pnl, signed=True), percent(b.day_return_pct),
            money(b.realized_pnl_today, signed=True), money(b.unrealized_pnl, signed=True),
            str(b.open_positions), str(b.open_orders), str(b.trades_today),
            Text(b.kill_switch, style=switch_style(b.kill_switch)), b.valuation.replace("_", " "),
        )  # fmt: skip
    return table


def _usage(book: BookRisk) -> Text:
    out = Text()
    for u in book.utilisation:
        if out:
            out.append("  ")
        used = f"{u.used:,.0f}/{u.limit:,.0f}" if u.unit == "count" else (
            f"{u.fraction:.0%}" if u.fraction is not None else "-")  # fmt: skip
        fraction = u.fraction or 0.0
        style = "red" if fraction >= 1 else ("yellow" if fraction >= WARN_AT else "")
        out.append(f"{u.label} ", style="dim")
        out.append(used, style=style)
    return out


def risk_strip(risk: RiskView) -> Table:
    table = Table(title="Risk", title_justify="left", box=box.SIMPLE_HEAD, expand=True)
    table.add_column("Book", style="bold")
    table.add_column("Switches")
    table.add_column("Limits used")
    table.add_column("Blocked today")
    for book in risk.books:
        tripped = [f"{k.scope.lower()}:{k.name} {k.state}" for k in book.kill_switches
                   if k.state != "ARMED"]  # fmt: skip
        switches = Text(", ".join(tripped), style="bold red") if tripped else Text(
            "ARMED", style="green")  # fmt: skip
        most = sorted(book.rejections_today.items(), key=lambda kv: -kv[1])[:3]
        blocked = Text(", ".join(f"{code} x{n}" for code, n in most) or "-",
                       style="yellow" if most else "dim")  # fmt: skip
        table.add_row(book.book_id, switches, _usage(book), blocked)
    return table


def positions_table(positions: Sequence[PositionRow]) -> Table:
    table = Table(title=f"Open positions ({len(positions)})", title_justify="left",
                  box=box.SIMPLE_HEAD, expand=True)  # fmt: skip
    for name in ("Book", "Symbol", "Qty", "Avg", "Mark", "Unrealized", "Stop", "Target",
                 "Strategy", "Held"):  # fmt: skip
        right = name in ("Qty", "Avg", "Mark", "Unrealized", "Stop", "Target", "Held")
        table.add_column(name, justify="right" if right else "left")
    for p in positions:
        table.add_row(
            p.book_id, Text(p.symbol, style="bold"), str(p.quantity), money(p.avg_price),
            money(p.mark), money(p.unrealized_pnl, signed=True), money(p.stop_price),
            money(p.target_price), p.strategy or "-",
            "-" if p.held_sessions is None else str(p.held_sessions),
        )  # fmt: skip
    if not positions:
        table.add_row(*(["-"] + [""] * 9))
    return table


def disposition_style(disposition: str) -> str:
    if disposition == Disposition.SUBMITTED:
        return "green"
    if disposition in (Disposition.VETOED, Disposition.RISK_REJECTED, Disposition.BROKER_REJECTED):
        return "red"
    if disposition == Disposition.UNKNOWN:
        return "yellow"
    return "dim"  # recorded, not traded: shadow strategies, regime gate, policy skips


def decisions_table(decisions: Sequence[DecisionRow]) -> Table:
    table = Table(title="Last decisions", title_justify="left", box=box.SIMPLE_HEAD,
                  expand=True)  # fmt: skip
    for name in ("Time", "Book", "Symbol", "Strategy", "Disposition", "Detail"):
        table.add_column(name)
    for d in decisions:
        table.add_row(clock_ist(d.ts), d.book_id, d.symbol, d.strategy,
                      Text(d.disposition, style=disposition_style(d.disposition)),
                      Text(d.detail[:DETAIL_WIDTH], style="dim"))  # fmt: skip
    if not decisions:
        table.add_row("-", "", "", "", "", "")
    return table


def alerts_table(alerts: Sequence[AlertRow]) -> Table:
    table = Table(title="Warnings today", title_justify="left", box=box.SIMPLE_HEAD,
                  expand=True, show_header=False)  # fmt: skip
    table.add_column("Time")
    table.add_column("Level")
    table.add_column("Message")
    for a in alerts:
        table.add_row(clock_ist(a.ts), Text(a.level, style="red" if a.level == "CRITICAL"
                                            else "yellow"), a.message[:120])  # fmt: skip
    if not alerts:
        table.add_row("-", "", Text("none", style="dim"))
    return table


def footer(frame: Frame) -> Text:
    ages = ", ".join(
        f"{source} {age:.0f}s old" for source, age in sorted(frame.quote_age_s.items())
    )
    out = Text(style="dim")
    out.append(f"quotes: {ages or '-'}   tasks: {', '.join(frame.tasks) or '-'}\n")
    out.append(f"Ctrl-C stops the session.  Halt every book: create {frame.halt_file} "
               "(content FLATTEN to also flatten).")  # fmt: skip
    return out


def render(frame: Frame) -> RenderableType:
    return Group(header(frame), books_table(frame.summary), risk_strip(frame.risk),
                 positions_table(frame.positions), decisions_table(frame.decisions),
                 alerts_table(frame.alerts), footer(frame))  # fmt: skip


# -- the view ------------------------------------------------------------------------------------


class TerminalView:
    """Paints the engine once a second (see ``src.engine.live._drive``) on its own read
    connection to the engine's store, like the web console."""

    def __init__(
        self, settings: Settings, *, console: Console | None = None, screen: bool = True
    ) -> None:
        self.settings = settings
        self.console = console or Console()
        self.screen = screen
        self.frame: Frame | None = None
        self._live: Live | None = None
        self._reader: EventStore | None = None
        self._queries: Queries | None = None

    async def __aenter__(self) -> TerminalView:
        # Transient: the live render is cleared at the end; __aexit__ prints the final state once.
        self._live = Live(Text("RakshaQuant: starting the session..."), console=self.console,
                          screen=self.screen, refresh_per_second=4, transient=True)  # fmt: skip
        self._live.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        if self.frame is not None:
            self.console.print(render(self.frame))  # the final state, on the normal screen
        if self._reader is not None:
            self._reader.close()
        self._reader, self._queries = None, None

    async def paint(self, engine: Engine) -> None:
        if self._queries is None:
            self._reader = EventStore(engine.store.path)
            self._queries = build_queries(self._reader, self.settings, clock=engine.clock)
        live = live_view(engine)  # on the event loop: the engine's in-memory state
        self.frame = await asyncio.to_thread(read_frame, self._queries, live,
                                             halt_file=str(self.settings.halt_file))  # fmt: skip
        if self._live is not None:
            self._live.update(render(self.frame), refresh=True)
