"""
RakshaQuant Professional Trading Dashboard

A feature-rich CLI dashboard for live trading monitoring with:
- Real-time market overview with all stocks
- Decision reasoning transparency
- Professional P&L tracking
- Visual indicators and progress bars
"""

import time
from dataclasses import dataclass, field
from datetime import datetime

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.utils.market_time import is_market_hours, now_ist

console = Console()


def _bar(fraction: float, width: int = 16, fill: str = "█", empty: str = "░") -> str:
    """Render a simple text progress bar for a 0..1 fraction."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return fill * filled + empty * (width - filled)


@dataclass
class TradingStats:
    """Real-time trading statistics."""

    # Session info
    session_start: datetime = field(default_factory=datetime.now)
    trading_mode: str = "paper"
    data_source: str = "simulated"

    # Account
    starting_balance: float = 1000000.0
    current_balance: float = 1000000.0

    # Trades
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # P&L
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0

    # Open positions
    open_positions: list = field(default_factory=list)

    # Agent activity
    cycles_run: int = 0
    signals_generated: int = 0
    signals_validated: int = 0
    signals_rejected: int = 0
    trades_approved: int = 0
    trades_risk_rejected: int = 0

    # Current regime
    current_regime: str = "unknown"
    regime_confidence: float = 0.0
    active_strategies: list = field(default_factory=list)

    # FinOps (LLM cost tracking, today / IST)
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0

    # Profit-target goal (month-to-date pace)
    goal_enabled: bool = False
    goal_feasible: bool = True
    goal_target_amount: float = 0.0
    goal_mtd_pnl: float = 0.0
    goal_expected_to_date: float = 0.0
    goal_on_pace: bool = True
    goal_status: str = ""

    # Market data
    market_quotes: dict = field(default_factory=dict)  # symbol -> quote dict
    top_movers: list = field(default_factory=list)

    # Decision info
    last_decision_reason: str = ""
    current_signal: dict = field(default_factory=dict)

    # Recent activity log
    activity_log: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def pnl_percent(self) -> float:
        if self.starting_balance == 0:
            return 0.0
        return (self.total_pnl / self.starting_balance) * 100

    def log_activity(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_log.append(
            {
                "time": timestamp,
                "level": level,
                "message": message,
            }
        )
        # Keep last 12 entries
        if len(self.activity_log) > 12:
            self.activity_log = self.activity_log[-12:]


def create_header(stats: TradingStats) -> Panel:
    """Create the header status bar: branding + mode/data/market badges + IST clock."""

    # Mode badge (paper = safe/green, live = red).
    mode_color = "green" if stats.trading_mode == "paper" else "red"
    mode_text = f"[bold {mode_color}]\\[{stats.trading_mode.upper()}][/]"

    # Data source badge.
    is_live_data = "live" in stats.data_source.lower() or "websocket" in stats.data_source.lower()
    data_color = "cyan" if is_live_data else "yellow"
    data_text = f"[{data_color}]\\[{stats.data_source.upper()}][/]"

    # Market open/closed (IST) + clock.
    market_open = is_market_hours()
    market_badge = (
        "[bold green]● NSE OPEN[/]" if market_open else "[dim]○ NSE CLOSED[/]"
    )
    clock = now_ist().strftime("%H:%M:%S IST")

    # Session time.
    elapsed = datetime.now() - stats.session_start
    hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=2)
    grid.add_column(justify="center", ratio=3)
    grid.add_column(justify="right", ratio=2)

    grid.add_row(
        f"{mode_text} {data_text}  {market_badge}",
        "[bold white on blue] 🛡️  RakshaQuant — Agentic NSE Trading [/]",
        f"[white]{clock}[/]  [dim]· up {elapsed_str}[/]",
    )

    return Panel(grid, style="bold", box=box.DOUBLE_EDGE, border_style="blue")


def create_account_panel(stats: TradingStats) -> Panel:
    """Create the account summary panel with visual indicators."""

    content = Text()

    # Balance with color
    balance_color = "green" if stats.current_balance >= stats.starting_balance else "red"
    content.append("\n  Balance: ", style="dim")
    content.append(f"Rs. {stats.current_balance:,.2f}\n", style=f"bold {balance_color}")

    # P&L with arrow indicator
    pnl_color = "green" if stats.total_pnl >= 0 else "red"
    pnl_arrow = "▲" if stats.total_pnl >= 0 else "▼"
    pnl_sign = "+" if stats.total_pnl >= 0 else ""

    content.append("  P&L: ", style="dim")
    content.append(f"{pnl_arrow} ", style=pnl_color)
    content.append(f"{pnl_sign}Rs. {stats.total_pnl:,.2f} ", style=f"bold {pnl_color}")
    content.append(f"({pnl_sign}{stats.pnl_percent:.2f}%)\n", style=pnl_color)

    content.append("\n")

    # Realized/Unrealized breakdown
    content.append("  Realized:   ", style="dim")
    r_color = "green" if stats.realized_pnl >= 0 else "red"
    content.append(f"Rs. {stats.realized_pnl:>10,.2f}\n", style=r_color)

    content.append("  Unrealized: ", style="dim")
    u_color = "green" if stats.unrealized_pnl >= 0 else "red"
    content.append(f"Rs. {stats.unrealized_pnl:>10,.2f}\n", style=u_color)

    # Best/Worst trade
    if stats.total_trades > 0:
        content.append("\n")
        content.append("  Best Trade:  ", style="dim")
        content.append(f"+Rs. {stats.best_trade:,.2f}\n", style="green")
        content.append("  Worst Trade: ", style="dim")
        content.append(f"Rs. {stats.worst_trade:,.2f}\n", style="red")

    # Profit-target goal pace
    if stats.goal_enabled:
        content.append("\n")
        content.append("  Monthly Goal: ", style="dim")
        content.append(f"Rs. {stats.goal_target_amount:,.0f}\n", style="bold white")
        if not stats.goal_feasible:
            content.append("  Pace: ", style="dim")
            content.append("⚠ target exceeds risk budget\n", style="bold yellow")
        else:
            pace_color = "green" if stats.goal_on_pace else "yellow"
            pace_icon = "▲" if stats.goal_on_pace else "▼"
            content.append("  Pace: ", style="dim")
            content.append(
                f"{pace_icon} Rs. {stats.goal_mtd_pnl:,.0f} / {stats.goal_expected_to_date:,.0f}\n",
                style=f"bold {pace_color}",
            )

    return Panel(content, title="[bold white]💰 Account[/]", border_style="blue", box=box.ROUNDED)


def create_trades_panel(stats: TradingStats) -> Panel:
    """Create the trades summary panel with visual win rate."""

    content = Text()

    # Trade counts
    content.append("\n  Total Trades: ", style="dim")
    content.append(f"{stats.total_trades}\n", style="bold white")

    content.append("  Winners:      ", style="dim")
    content.append(f"{stats.winning_trades}\n", style="bold green")

    content.append("  Losers:       ", style="dim")
    content.append(f"{stats.losing_trades}\n", style="bold red")

    content.append("\n")

    # Win rate with visual bar
    content.append("  Win Rate: ", style="dim")
    wr = stats.win_rate
    wr_color = "green" if wr >= 50 else "yellow" if wr >= 40 else "red"
    content.append(f"{wr:.1f}%\n", style=f"bold {wr_color}")

    # Visual win rate bar
    bar_width = 20
    filled = int((wr / 100) * bar_width)
    content.append("  [", style="dim")
    content.append("█" * filled, style="green")
    content.append("░" * (bar_width - filled), style="dim")
    content.append("]\n", style="dim")

    return Panel(content, title="[bold white]📊 Trades[/]", border_style="green", box=box.ROUNDED)


def create_regime_panel(stats: TradingStats) -> Panel:
    """Create the market regime panel with visual indicators."""

    regime_styles = {
        "trending_up": ("green", "🟢", "BULL"),
        "trending_down": ("red", "🔴", "BEAR"),
        "ranging": ("yellow", "🟡", "RANGE"),
        "volatile": ("magenta", "🟣", "VOLATILE"),
        "unknown": ("dim", "⚪", "UNKNOWN"),
    }

    color, icon, label = regime_styles.get(stats.current_regime, ("dim", "⚪", "UNKNOWN"))

    content = Text()
    content.append(f"\n  {icon} ", style=color)
    content.append(f"{label}\n", style=f"bold {color}")

    # Confidence bar
    conf = stats.regime_confidence
    conf_color = "green" if conf >= 0.7 else "yellow" if conf >= 0.5 else "red"
    bar_width = 15
    filled = int(conf * bar_width)

    content.append("\n  Confidence: ", style="dim")
    content.append(f"{conf:.0%}\n", style=f"bold {conf_color}")
    content.append("  [", style="dim")
    content.append("█" * filled, style=conf_color)
    content.append("░" * (bar_width - filled), style="dim")
    content.append("]\n", style="dim")

    # Active strategies
    content.append("\n  Strategies:\n", style="dim")
    if stats.active_strategies:
        for strat in stats.active_strategies[:3]:
            content.append(f"    • {strat}\n", style="cyan")
    else:
        content.append("    (none active)\n", style="dim italic")

    return Panel(
        content, title="[bold white]📈 Market Regime[/]", border_style="cyan", box=box.ROUNDED
    )


def create_market_overview(stats: TradingStats) -> Panel:
    """Create market overview showing all monitored stocks."""

    if not stats.market_quotes:
        content = Text("\n  Waiting for market data...\n", style="dim italic")
        return Panel(
            content,
            title="[bold white]🌐 Market Overview[/]",
            border_style="yellow",
            box=box.ROUNDED,
        )

    table = Table(box=box.SIMPLE, show_header=True, expand=True, padding=(0, 1))
    table.add_column("Symbol", style="bold", width=10)
    table.add_column("LTP", justify="right", width=10)
    table.add_column("Chg%", justify="right", width=8)
    table.add_column("", width=4)  # Trend indicator

    # Sort by change percent
    sorted_quotes = sorted(
        stats.market_quotes.items(),
        key=lambda x: x[1].get("change_percent", 0),
        reverse=True,
    )

    for symbol, quote in sorted_quotes[:8]:  # Top 8 stocks
        ltp = quote.get("last_price", 0)
        chg = quote.get("change_percent", 0)

        chg_color = "green" if chg > 0 else "red" if chg < 0 else "white"
        trend = "▲" if chg > 0 else "▼" if chg < 0 else "─"

        table.add_row(
            symbol,
            f"Rs.{ltp:,.0f}",
            f"[{chg_color}]{chg:+.2f}%[/]",
            f"[{chg_color}]{trend}[/]",
        )

    return Panel(
        table, title="[bold white]🌐 Market Overview[/]", border_style="yellow", box=box.ROUNDED
    )


def create_decision_panel(stats: TradingStats) -> Panel:
    """Create panel showing current decision reasoning."""

    content = Text()

    if stats.current_signal:
        sig = stats.current_signal
        signal_type = sig.get("signal_type", "N/A")
        symbol = sig.get("symbol", "N/A")
        strategy = sig.get("strategy", "N/A")
        confidence = sig.get("confidence", 0)

        signal_color = "green" if signal_type == "BUY" else "red"

        content.append("\n  Current Signal:\n", style="dim")
        content.append(f"    {signal_type} ", style=f"bold {signal_color}")
        content.append(f"{symbol}\n", style="bold white")

        content.append("    Strategy: ", style="dim")
        content.append(f"{strategy}\n", style="cyan")

        content.append("    Confidence: ", style="dim")
        content.append(f"{confidence:.0%}\n", style="bold")
    else:
        content.append("\n  No active signal\n", style="dim italic")

    if stats.last_decision_reason:
        content.append("\n  Decision Reason:\n", style="dim")
        # Wrap long text
        reason = stats.last_decision_reason[:100]
        content.append(f"    {reason}\n", style="italic")

    return Panel(
        content, title="[bold white]🧠 AI Decision[/]", border_style="magenta", box=box.ROUNDED
    )


def create_agent_panel(stats: TradingStats) -> Panel:
    """Create the agent activity panel."""

    table = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Metric", style="dim", width=16)
    table.add_column("Value", justify="right", width=6)

    table.add_row("Cycles Run", f"[bold]{stats.cycles_run}[/]")
    table.add_row("Signals Gen", f"[white]{stats.signals_generated}[/]")
    table.add_row("Validated", f"[green]{stats.signals_validated}[/]")
    table.add_row("Rejected", f"[red]{stats.signals_rejected}[/]")
    table.add_row("Risk Blocked", f"[yellow]{stats.trades_risk_rejected}[/]")

    # Approval rate
    if stats.signals_generated > 0:
        approval_rate = (stats.signals_validated / stats.signals_generated) * 100
        table.add_row("Approval Rate", f"[cyan]{approval_rate:.0f}%[/]")

    return Panel(
        table, title="[bold white]🤖 Agent Activity[/]", border_style="magenta", box=box.ROUNDED
    )


def create_status_panel(stats: TradingStats) -> Panel:
    """Session footer: today's LLM spend (FinOps) + profit-goal pace + controls."""

    content = Text()

    content.append("\n  LLM today (FinOps)\n", style="bold")
    content.append(f"    Calls:  {stats.llm_calls}\n", style="dim")
    content.append(f"    Tokens: {stats.llm_tokens:,}\n", style="dim")
    content.append("    Cost:   ", style="dim")
    content.append(f"${stats.llm_cost_usd:.4f}\n", style="cyan")

    if stats.goal_enabled:
        content.append("\n  Profit goal\n", style="bold")
        if not stats.goal_feasible:
            content.append("    ⚠ target exceeds risk budget\n", style="yellow")
        else:
            target = stats.goal_expected_to_date or 0.0
            frac = (stats.goal_mtd_pnl / target) if target > 0 else 0.0
            pace_color = "green" if stats.goal_on_pace else "yellow"
            content.append(
                f"    Rs.{stats.goal_mtd_pnl:,.0f} / {stats.goal_expected_to_date:,.0f}\n",
                style=pace_color,
            )
            content.append(f"    {_bar(max(0.0, frac), width=14)}\n", style=pace_color)
            content.append(
                f"    {'▲ on pace' if stats.goal_on_pace else '▼ behind pace'}\n",
                style=pace_color,
            )

    content.append("\n  Ctrl+C to stop\n", style="dim italic")

    return Panel(
        content, title="[bold white]⚙️  Session & Cost[/]", border_style="cyan", box=box.ROUNDED
    )


def create_positions_panel(stats: TradingStats) -> Panel:
    """Create the open positions panel."""

    if not stats.open_positions:
        content = Text("\n  No open positions\n", style="dim italic")
        return Panel(
            content, title="[bold white]📋 Open Positions[/]", border_style="white", box=box.ROUNDED
        )

    table = Table(box=box.SIMPLE, expand=True, show_header=True, padding=(0, 1))
    table.add_column("Symbol", style="bold", width=10)
    table.add_column("Side", width=5)
    table.add_column("Qty", justify="right", width=5)
    table.add_column("Entry", justify="right", width=12)
    table.add_column("P&L", justify="right", width=12)

    for pos in stats.open_positions[:5]:
        pnl = pos.get("pnl", 0)
        pnl_color = "green" if pnl >= 0 else "red"
        pnl_sign = "+" if pnl >= 0 else ""

        side = pos.get("side", "N/A")
        side_color = "green" if side == "BUY" else "red"

        table.add_row(
            pos.get("symbol", "N/A"),
            f"[{side_color}]{side}[/]",
            str(pos.get("qty", 0)),
            f"Rs.{pos.get('entry', 0):,.2f}",
            f"[{pnl_color}]{pnl_sign}Rs.{pnl:,.2f}[/]",
        )

    return Panel(
        table, title="[bold white]📋 Open Positions[/]", border_style="white", box=box.ROUNDED
    )


def create_activity_panel(stats: TradingStats) -> Panel:
    """Create the activity log panel."""

    if not stats.activity_log:
        content = Text("\n  Waiting for activity...\n", style="dim italic")
        return Panel(
            content, title="[bold white]📜 Activity Log[/]", border_style="white", box=box.ROUNDED
        )

    lines = []
    level_styles = {
        "INFO": ("blue", "ℹ"),
        "SUCCESS": ("green", "✓"),
        "WARNING": ("yellow", "⚠"),
        "ERROR": ("red", "✗"),
        "TRADE": ("cyan", "💹"),
    }

    for entry in stats.activity_log[-10:]:
        color, icon = level_styles.get(entry["level"], ("white", "•"))
        lines.append(f"[dim]{entry['time']}[/] [{color}]{icon}[/] {entry['message']}")

    content = "\n".join(lines)

    return Panel(
        content, title="[bold white]📜 Activity Log[/]", border_style="white", box=box.ROUNDED
    )


def create_dashboard_layout(stats: TradingStats) -> Layout:
    """Create the full professional dashboard layout."""

    layout = Layout()

    # Main structure
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=14),
    )

    # Body split into 3 columns
    layout["body"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="center", ratio=1),
        Layout(name="right", ratio=1),
    )

    # Left column: Account + Trades
    layout["left"].split_column(
        Layout(name="account"),
        Layout(name="trades"),
    )

    # Center column: Market Overview + Positions
    layout["center"].split_column(
        Layout(name="market"),
        Layout(name="positions"),
    )

    # Right column: Regime + Decision + Agent
    layout["right"].split_column(
        Layout(name="regime"),
        Layout(name="decision"),
        Layout(name="agent"),
    )

    # Footer: activity log (wide) + a session/cost/goal status panel (narrow)
    layout["footer"].split_row(
        Layout(name="activity", ratio=3),
        Layout(name="status", ratio=1),
    )

    # Populate panels
    layout["header"].update(create_header(stats))
    layout["account"].update(create_account_panel(stats))
    layout["trades"].update(create_trades_panel(stats))
    layout["market"].update(create_market_overview(stats))
    layout["positions"].update(create_positions_panel(stats))
    layout["regime"].update(create_regime_panel(stats))
    layout["decision"].update(create_decision_panel(stats))
    layout["agent"].update(create_agent_panel(stats))
    layout["activity"].update(create_activity_panel(stats))
    layout["status"].update(create_status_panel(stats))

    return layout


class TradingDashboard:
    """Live trading dashboard manager."""

    def __init__(self):
        self.stats = TradingStats()
        self.live = None
        self.running = False

    def start(
        self, balance: float = 1000000.0, mode: str = "paper", data_source: str = "simulated"
    ):
        """Start the dashboard."""
        self.stats.starting_balance = balance
        self.stats.current_balance = balance
        self.stats.trading_mode = mode
        self.stats.data_source = data_source
        self.stats.session_start = datetime.now()
        self.running = True

        self.stats.log_activity("Dashboard started", "INFO")
        self.stats.log_activity(f"Mode: {mode.upper()}", "INFO")
        self.stats.log_activity(f"Data: {data_source.upper()}", "INFO")

    def update_regime(self, regime: str, confidence: float, strategies: list):
        """Update market regime info."""
        self.stats.current_regime = regime
        self.stats.regime_confidence = confidence
        self.stats.active_strategies = strategies
        self.stats.log_activity(f"Regime: {regime} ({confidence:.0%})", "INFO")

    def update_market_data(self, quotes: dict):
        """Update market quotes."""
        self.stats.market_quotes = quotes

    def set_current_signal(self, signal_type: str, symbol: str, strategy: str, confidence: float):
        """Set the current trading signal."""
        self.stats.current_signal = {
            "signal_type": signal_type,
            "symbol": symbol,
            "strategy": strategy,
            "confidence": confidence,
        }

    def set_decision_reason(self, reason: str):
        """Set the decision reasoning."""
        self.stats.last_decision_reason = reason

    def log_signal(self, symbol: str, signal_type: str, strategy: str, validated: bool):
        """Log a signal."""
        self.stats.signals_generated += 1
        if validated:
            self.stats.signals_validated += 1
            self.stats.log_activity(f"✓ {signal_type} {symbol} [{strategy}]", "SUCCESS")
        else:
            self.stats.signals_rejected += 1
            self.stats.log_activity(f"✗ {signal_type} {symbol} rejected", "WARNING")

    def log_trade(self, symbol: str, side: str, qty: int, price: float, approved: bool):
        """Log a trade decision."""
        if approved:
            self.stats.trades_approved += 1
            self.stats.log_activity(f"TRADE: {side} {qty}x {symbol} @ Rs.{price:,.2f}", "TRADE")
        else:
            self.stats.trades_risk_rejected += 1
            self.stats.log_activity(f"BLOCKED: {side} {symbol} by risk", "WARNING")

    def add_position(self, symbol: str, side: str, qty: int, entry_price: float):
        """Add an open position."""
        self.stats.open_positions.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "entry": entry_price,
                "pnl": 0.0,
            }
        )

    def close_trade(self, pnl: float):
        """Record a closed trade."""
        self.stats.total_trades += 1
        self.stats.realized_pnl += pnl
        self.stats.current_balance += pnl

        # Track best/worst
        if pnl > self.stats.best_trade:
            self.stats.best_trade = pnl
        if pnl < self.stats.worst_trade:
            self.stats.worst_trade = pnl

        if pnl >= 0:
            self.stats.winning_trades += 1
            self.stats.log_activity(f"Trade closed: +Rs.{pnl:,.2f}", "SUCCESS")
        else:
            self.stats.losing_trades += 1
            self.stats.log_activity(f"Trade closed: Rs.{pnl:,.2f}", "ERROR")

    def increment_cycle(self):
        """Increment cycle counter."""
        self.stats.cycles_run += 1
        self.stats.log_activity(f"Cycle #{self.stats.cycles_run} complete", "INFO")

    def render(self) -> Layout:
        """Render the dashboard."""
        return create_dashboard_layout(self.stats)


def demo_dashboard():
    """Demo the dashboard with simulated data."""

    dashboard = TradingDashboard()
    dashboard.start(balance=1000000.0, mode="paper", data_source="simulated")

    # Sample market data
    sample_quotes = {
        "RELIANCE": {"last_price": 2485.50, "change_percent": 1.25},
        "TCS": {"last_price": 4150.00, "change_percent": -0.85},
        "HDFCBANK": {"last_price": 1680.25, "change_percent": 0.45},
        "INFY": {"last_price": 1845.00, "change_percent": -1.20},
        "SBIN": {"last_price": 785.50, "change_percent": 2.15},
        "ITC": {"last_price": 465.00, "change_percent": 0.65},
        "ICICIBANK": {"last_price": 1275.00, "change_percent": -0.35},
        "BHARTIARTL": {"last_price": 1620.00, "change_percent": -1.75},
    }

    console.print("[bold green]Starting Professional Dashboard Demo...[/]\n")
    console.print("[dim]Press Ctrl+C to exit[/]\n")
    time.sleep(1)

    with Live(dashboard.render(), console=console, refresh_per_second=2) as live:
        try:
            dashboard.update_market_data(sample_quotes)

            time.sleep(2)
            dashboard.update_regime("trending_up", 0.72, ["momentum", "trend_following"])
            live.update(dashboard.render())

            time.sleep(2)
            dashboard.set_current_signal("BUY", "SBIN", "momentum", 0.68)
            dashboard.set_decision_reason(
                "Strong upward momentum (+2.15%) with RSI oversold bounce"
            )
            live.update(dashboard.render())

            time.sleep(2)
            dashboard.log_signal("SBIN", "BUY", "momentum", True)
            live.update(dashboard.render())

            time.sleep(2)
            dashboard.log_trade("SBIN", "BUY", 50, 785.50, True)
            dashboard.add_position("SBIN", "BUY", 50, 785.50)
            live.update(dashboard.render())

            time.sleep(2)
            dashboard.increment_cycle()
            live.update(dashboard.render())

            time.sleep(3)
            dashboard.close_trade(750.0)
            dashboard.stats.open_positions = []
            live.update(dashboard.render())

            while True:
                time.sleep(0.5)
                live.update(dashboard.render())

        except KeyboardInterrupt:
            console.print("\n[yellow]Dashboard stopped[/]")


if __name__ == "__main__":
    demo_dashboard()
