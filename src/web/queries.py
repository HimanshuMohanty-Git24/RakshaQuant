"""
The web API's read side (plan M9.2): projections and events from the event store, turned into
:mod:`src.web.models`. Everything here is synchronous and side-effect free, so the server runs
it in a worker thread on its own read connection (WAL readers never block the engine).

What only the running engine knows (live marks, valuations, its tasks, quote ages) is captured
on the event loop as a :class:`LiveView` first and passed in; without an engine the views fall
back to the last recorded ``MarkToMarket``.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import platform
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from src.config.limits import RiskLimits
from src.decision_models.calibration import CalibrationMap
from src.domain.calendar import CalendarCoverageError, NSECalendar
from src.domain.clock import Clock, now_ist
from src.domain.events import (
    Alert,
    Event,
    FillReceived,
    Heartbeat,
    LLMOutcome,
    MarkToMarket,
    OrderSubmitted,
    ProcessStarted,
    ReconciliationResult,
    RegimeComputed,
    SessionStateChanged,
    SignalDisposition,
    SignalGenerated,
    payload_json,
)
from src.domain.types import (
    Bar,
    Instrument,
    KillScope,
    OrderStatus,
    Quote,
    RiskDecision,
    RiskOutcome,
    TypedEvent,
)
from src.engine.lifecycle import LifecycleConfig, build_schedule
from src.engine.market import INDEX_KEY
from src.evaluation.daily_report import resting, slippage_bps, veto_precision_of
from src.evaluation.experiment import ExperimentConfig
from src.llm.prompts import templates
from src.llm.registry import role_configs
from src.oms.exit_manager import ManagedPosition
from src.ops.logging_config import redact
from src.risk.events import EventBlock, EventRules, event_blocks
from src.risk.snapshot import RiskSnapshot
from src.risk.utilisation import Usage, sector_usage, utilisation
from src.store.event_store import EventStore
from src.store.kv import KVStateStore
from src.store.sqlite import load_migrations
from src.store.tape import read_bars, read_quotes
from src.utils.market_time import IST
from src.web.models import (
    AlertRow,
    BarMarker,
    BenchmarkPoint,
    BookComparison,
    BookRisk,
    BookSummary,
    BooksView,
    CalibrationView,
    ConfigView,
    DecisionModelStats,
    DecisionRow,
    Document,
    EquityPoint,
    EquitySeries,
    EquityView,
    EventBlockRow,
    Execution,
    FillRow,
    KillSwitchRow,
    Lineage,
    LineageEvent,
    LLMCallRow,
    LogLine,
    ModelHealth,
    OrderRow,
    PositionRow,
    ReconcileRow,
    ReportBook,
    ReportSummary,
    RiskView,
    RoleModels,
    ScheduleStep,
    SessionInfo,
    SpendRow,
    SpendView,
    Summary,
    SystemView,
    TradeRow,
    TypedEventRow,
    Utilisation,
    VetoPrecision,
    WatchRow,
)

Valuation = Literal["live", "last_mark", "none"]
GroupBy = Literal["role", "book", "day", "model"]
_TERMINAL = tuple(s.value for s in OrderStatus if s.is_terminal)
_ERRORS = tuple(o.value for o in LLMOutcome if o is not LLMOutcome.OK)
MAX_ROWS = 1000
REPO = Path(__file__).resolve().parents[2]
PREREGISTRATION = REPO / "docs" / "experiments" / "2026-10-month1-preregistration.md"
_PROMPTS = {t.name: t for t in (templates.VETO_V1, templates.REVIEW_V1, templates.EXPLAIN_V1,
                                templates.LABEL_ANNOUNCEMENT_V1)}  # fmt: skip


@dataclass(frozen=True)
class LiveView:
    """The running engine's in-memory state, captured on the event loop."""

    valuations: Mapping[str, MarkToMarket] = field(default_factory=dict)
    marks: Mapping[str, Decimal] = field(default_factory=dict)
    tasks: tuple[str, ...] = ()
    quote_age_s: Mapping[str, float] = field(default_factory=dict)
    risk: Mapping[str, RiskSnapshot] = field(default_factory=dict)  # per book (gate's view)
    managed: Mapping[str, Mapping[str, ManagedPosition]] = field(default_factory=dict)
    instruments: Mapping[str, Instrument] = field(default_factory=dict)
    quotes: Mapping[str, Quote] = field(default_factory=dict)
    index_closes: Mapping[date, float] = field(default_factory=dict)


def symbol_of(instrument_key: str) -> str:
    return instrument_key.rsplit(":", 1)[-1]


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 1)


class Queries:
    def __init__(
        self,
        store: EventStore,
        *,
        settings: Any,
        experiment: ExperimentConfig,
        limits: RiskLimits,
        calendar: NSECalendar,
        clock: Clock,
        reports_dir: Path,
        demo: bool = False,
        read_only: bool = False,
        tape_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.experiment = experiment
        self.limits = limits
        self.calendar = calendar
        self.clock = clock
        self.reports_dir = reports_dir
        self.demo = demo
        self.read_only = read_only
        self.tape_dir = tape_dir if tape_dir is not None else Path(settings.tape_dir)

    @property
    def books(self) -> list[str]:
        return list(self.experiment.books)

    def today(self) -> date:
        return now_ist(self.clock).date()

    # -- helpers -------------------------------------------------------------------------------

    def _latest(self, event_type: str, *, book: str | None = None) -> Event | None:
        sql = "SELECT MAX(seq) AS seq FROM events WHERE type = ?"
        params: list[Any] = [event_type]
        if book is not None:
            sql += " AND book_id = ?"
            params.append(book)
        seq = self.store.query(sql, params)[0]["seq"]
        if seq is None:
            return None
        found = self.store.read(since_seq=int(seq) - 1, limit=1)
        return found[0] if found else None

    def _valuation(self, book: str, live: LiveView | None) -> tuple[MarkToMarket | None,
                                                                   datetime | None, Valuation]:  # fmt: skip
        if live is not None and book in live.valuations:
            return live.valuations[book], self.clock.now(), "live"
        last = self._latest(MarkToMarket.event_type, book=book)
        if last is not None and isinstance(last.payload, MarkToMarket):
            return last.payload, last.ts_utc, "last_mark"
        return None, None, "none"

    def _global_switch(self, book: str) -> str:
        rows = self.store.query(
            "SELECT state FROM kill_switches WHERE book_id = ? AND scope = ? AND name = ?",
            (book, KillScope.GLOBAL.value, KillScope.GLOBAL.value),
        )
        return str(rows[0]["state"]) if rows else "ARMED"

    def _closed_today(self, book: str, day: date) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT t.net_pnl FROM trades t JOIN events e ON e.seq = t.seq"
            " WHERE t.book_id = ? AND e.ist_date = ?",
            (book, day.isoformat()),
        )

    # -- summary ---------------------------------------------------------------------------------

    def summary(self, live: LiveView | None, *, running: bool) -> Summary:
        today = self.today()
        session_event = self._latest(SessionStateChanged.event_type)
        session = None
        if session_event is not None and isinstance(session_event.payload, SessionStateChanged):
            p = session_event.payload
            session = SessionInfo(date=p.session_date, state=p.current.value)
        books = []
        for book in self.books:
            mtm, at, kind = self._valuation(book, live)
            closed = self._closed_today(book, today)
            sod = mtm.equity - mtm.day_pnl if mtm else None
            books.append(BookSummary(
                book_id=book, advisor=self.experiment.books[book].advisor,
                equity=mtm.equity if mtm else None, cash=mtm.cash if mtm else None,
                unrealized_pnl=mtm.unrealized_pnl if mtm else None,
                day_pnl=mtm.day_pnl if mtm else None, valued_at=at, valuation=kind,
                realized_pnl_today=sum((Decimal(r["net_pnl"]) for r in closed), Decimal(0)),
                open_positions=self.store.query(
                    "SELECT COUNT(*) AS n FROM positions WHERE book_id = ? AND quantity != 0",
                    (book,))[0]["n"],
                open_orders=self.store.query(
                    f"SELECT COUNT(*) AS n FROM orders WHERE book_id = ? AND status NOT IN"
                    f" ({','.join('?' * len(_TERMINAL))})", (book, *_TERMINAL))[0]["n"],
                trades_today=len(closed), kill_switch=self._global_switch(book),
                day_return_pct=round(float(mtm.day_pnl / sod * 100), 3)
                if mtm is not None and sod else None,
            ))  # fmt: skip
        return Summary(
            environment=self.settings.environment, demo=self.demo, running=running,
            experiment=self.experiment.experiment, session=session,
            market_open=self.calendar.is_market_open(self.clock.now()), now=self.clock.now(),
            last_seq=self.store.last_seq(), books=books,
            schedule=self._schedule(session.date if session else today),
        )  # fmt: skip

    def _schedule(self, day: date) -> list[ScheduleStep]:
        start, end = self.experiment.window()
        try:
            schedule = build_schedule(self.calendar, day, LifecycleConfig(start, end))
        except CalendarCoverageError:
            return []
        if schedule is None:
            return []
        return [ScheduleStep(state=s.value, at=at) for s, at in schedule.transitions()]

    # -- blotter ---------------------------------------------------------------------------------

    def positions(self, live: LiveView | None, *, book: str | None) -> list[PositionRow]:
        sql, params = "SELECT * FROM positions WHERE quantity != 0", []
        if book is not None:
            sql, params = sql + " AND book_id = ?", [book]
        marks = live.marks if live is not None else {}
        managed: dict[str, Mapping[str, ManagedPosition]] = {}
        out = []
        for r in self.store.query(sql + " ORDER BY book_id, instrument_key", params):
            b, key = r["book_id"], r["instrument_key"]
            avg, mark = _dec(r["avg_price"]), marks.get(key)
            unrealized = (
                (mark - avg) * r["quantity"] if mark is not None and avg is not None else None
            )
            if b not in managed:
                managed[b] = live.managed.get(b, {}) if live is not None else self._managed(b)
            m = managed[b].get(f"{key}|{r['product']}")
            snapshot = live.risk.get(b) if live is not None else None
            resting = (
                snapshot.positions[key].stop_price
                if snapshot and key in snapshot.positions
                else None
            )
            out.append(PositionRow(
                book_id=b, instrument_key=key, symbol=symbol_of(key), product=r["product"],
                quantity=r["quantity"], avg_price=avg, realized_pnl=Decimal(r["realized_pnl"]),
                mark=mark, unrealized_pnl=unrealized, updated_ts=_dt(r["updated_ts"]),
                strategy=m.strategy if m else None,
                entry_decision_id=m.entry_decision_id if m else None,
                stop_price=resting if resting is not None else (m.stop_price if m else None),
                target_price=m.target_price if m else None,
                entered_on=m.entered_on if m else None,
                held_sessions=self._sessions_since(m.entered_on) if m else None,
            ))  # fmt: skip
        return out

    def _managed(self, book: str) -> dict[str, ManagedPosition]:
        """The exit manager's persisted positions (stops, targets, entry dates)."""
        raw = KVStateStore(self.store, book, "exit_manager").load()
        if not raw:
            return {}
        try:
            positions = json.loads(raw).get("positions", {})
            return {k: ManagedPosition.model_validate(v) for k, v in positions.items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def _sessions_since(self, day: date) -> int | None:
        try:
            return max(0, len(self.calendar.trading_days(day, self.today())) - 1)
        except CalendarCoverageError:
            return None

    def orders(self, *, book: str | None, status: str | None, day: date | None,
               limit: int, symbol: str | None = None) -> list[OrderRow]:  # fmt: skip
        clauses, params = self._filters(book=book, day=day, alias="o", symbol=symbol)
        if status is not None:
            clauses.append("o.status = ?")
            params.append(status)
        rows = self.store.query(
            "SELECT o.*, e.ist_date FROM orders o JOIN events e ON e.seq = o.created_seq"
            f"{_where(clauses)} ORDER BY o.updated_seq DESC LIMIT ?", [*params, limit],
        )  # fmt: skip
        return [OrderRow(
            client_order_id=r["client_order_id"], book_id=r["book_id"],
            decision_id=r["decision_id"], intent_id=r["intent_id"],
            instrument_key=r["instrument_key"], symbol=symbol_of(r["instrument_key"]),
            strategy=r["strategy"], side=r["side"], product=r["product"],
            order_type=r["order_type"], kind=r["kind"], reason=r["reason"],
            quantity=r["quantity"], status=r["status"], filled_qty=r["filled_qty"],
            avg_fill_price=_dec(r["avg_fill_price"]), broker_order_id=r["broker_order_id"],
            last_message=r["last_message"], ist_date=date.fromisoformat(r["ist_date"]),
            updated_ts=_dt(r["updated_ts"]),
        ) for r in rows]  # fmt: skip

    def fills(self, *, book: str | None, day: date | None, limit: int,
              symbol: str | None = None) -> list[FillRow]:  # fmt: skip
        clauses, params = self._filters(book=book, day=day, alias="f", symbol=symbol)
        rows = self.store.query(
            f"SELECT f.* FROM fills f JOIN events e ON e.seq = f.seq{_where(clauses)}"
            " ORDER BY f.seq DESC LIMIT ?", [*params, limit],
        )  # fmt: skip
        return [FillRow(
            fill_id=r["fill_id"], client_order_id=r["client_order_id"], book_id=r["book_id"],
            decision_id=r["decision_id"], instrument_key=r["instrument_key"],
            symbol=symbol_of(r["instrument_key"]), side=r["side"], quantity=r["quantity"],
            price=Decimal(r["price"]), charges=Decimal(r["charges"]), ts=_dt(r["ts_utc"]),
        ) for r in rows]  # fmt: skip

    def trades(self, *, book: str | None, day: date | None, strategy: str | None,
               limit: int, symbol: str | None = None) -> list[TradeRow]:  # fmt: skip
        clauses, params = self._filters(book=book, day=day, alias="t", symbol=symbol)
        if strategy is not None:
            clauses.append("t.strategy = ?")
            params.append(strategy)
        rows = self.store.query(
            f"SELECT t.* FROM trades t JOIN events e ON e.seq = t.seq{_where(clauses)}"
            " ORDER BY t.seq DESC LIMIT ?", [*params, limit],
        )  # fmt: skip
        return [TradeRow(
            trade_id=r["trade_id"], book_id=r["book_id"], decision_id=r["decision_id"],
            exit_decision_id=r["exit_decision_id"], instrument_key=r["instrument_key"],
            symbol=symbol_of(r["instrument_key"]), strategy=r["strategy"], side=r["side"],
            quantity=r["quantity"], entry_price=Decimal(r["entry_price"]),
            exit_price=Decimal(r["exit_price"]), entry_ts=_dt(r["entry_ts"]),
            exit_ts=_dt(r["exit_ts"]), gross_pnl=Decimal(r["gross_pnl"]),
            charges=Decimal(r["charges"]), net_pnl=Decimal(r["net_pnl"]),
            exit_reason=r["exit_reason"],
        ) for r in rows]  # fmt: skip

    @staticmethod
    def _filters(*, book: str | None, day: date | None, alias: str,
                 symbol: str | None = None) -> tuple[list[str], list[Any]]:  # fmt: skip
        clauses: list[str] = []
        params: list[Any] = []
        if book is not None:
            clauses.append(f"{alias}.book_id = ?")
            params.append(book)
        if symbol is not None:  # the key's last segment, exactly (LIKE wildcards escaped)
            escaped = symbol.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            clauses.append(f"{alias}.instrument_key LIKE ? ESCAPE '!'")
            params.append(f"%:{escaped}")
        if day is not None:
            clauses.append("e.ist_date = ?")
            params.append(day.isoformat())
        return clauses, params

    # -- decisions -------------------------------------------------------------------------------

    def decisions(self, *, book: str | None, symbol: str | None, strategy: str | None,
                  outcome: str | None, day: date | None, limit: int) -> list[DecisionRow]:  # fmt: skip
        events = self.store.read(types=[SignalDisposition.event_type], book_id=book,
                                 symbol=symbol, ist_date=day)  # fmt: skip
        out: list[DecisionRow] = []
        for e in reversed(events):
            p = e.payload
            if not isinstance(p, SignalDisposition):
                continue
            if strategy is not None and p.strategy != strategy:
                continue
            if outcome is not None and p.disposition.value != outcome:
                continue
            out.append(DecisionRow(
                seq=e.seq or 0, ts=e.ts_utc, decision_id=p.decision_id, signal_id=p.signal_id,
                book_id=p.book_id, instrument_key=p.instrument_key,
                symbol=symbol_of(p.instrument_key), strategy=p.strategy,
                disposition=p.disposition.value, detail=p.detail,
                client_order_id=p.client_order_id,
            ))  # fmt: skip
            if len(out) >= limit:
                break
        return out

    def lineage(self, decision_id: str) -> Lineage | None:
        events = self.store.read(decision_id=decision_id)
        if not events:
            return None
        exits = sorted({r["exit_decision_id"] for r in self.store.query(
            "SELECT exit_decision_id FROM trades WHERE decision_id = ?"
            " AND exit_decision_id IS NOT NULL", (decision_id,))} - {decision_id})  # fmt: skip
        for exit_id in exits:
            events += self.store.read(decision_id=exit_id)
        events.sort(key=lambda e: e.seq or 0)
        regimes = [e.payload for e in self.store.read(types=[RegimeComputed.event_type],
                                                       ist_date=events[0].ist_date)
                   if isinstance(e.payload, RegimeComputed)]  # fmt: skip
        return Lineage(decision_id=decision_id, exit_decision_ids=exits, events=[
            LineageEvent(seq=e.seq or 0, ts=e.ts_utc, type=e.type, decision_id=e.decision_id,
                         book_id=e.book_id, source=e.source,
                         data=json.loads(payload_json(e.payload)))
            for e in events
        ], regime=regimes[-1].label.value if regimes else None,
            executions=self._executions(events))  # fmt: skip

    def _executions(self, events: Sequence[Event]) -> list[Execution]:
        fills: dict[str, list[FillReceived]] = defaultdict(list)
        arrival: dict[str, Decimal] = {}
        submitted = []
        for e in events:
            p = e.payload
            if isinstance(p, OrderSubmitted):
                submitted.append(p.order)
            elif isinstance(p, FillReceived):
                fills[p.fill.client_order_id].append(p)
            elif isinstance(p, RiskDecision) and p.client_order_id and p.ref_price is not None:
                arrival[p.client_order_id] = p.ref_price
        if not submitted:
            return []
        coids = [o.client_order_id for o in submitted]
        status = {r["client_order_id"]: r["status"] for r in self.store.query(
            f"SELECT client_order_id, status FROM orders WHERE client_order_id IN"
            f" ({','.join('?' * len(coids))})", coids)}  # fmt: skip
        out = []
        for order in submitted:
            got = fills.get(order.client_order_id, [])
            avg = got[-1].order_avg_price if got else None
            side = order.intent.side
            decision = order.intent.decision_price
            ref = (
                None
                if resting(order)
                else arrival.get(order.client_order_id) or order.arrival_price
            )
            out.append(Execution(
                book_id=order.intent.book_id, client_order_id=order.client_order_id,
                kind=order.intent.kind.value, side=side.value, quantity=order.quantity,
                filled_qty=got[-1].order_filled_qty if got else 0,
                status=status.get(order.client_order_id, order.status.value),
                decision_price=decision, arrival_price=ref, avg_fill_price=avg,
                slippage_vs_decision_bps=slippage_bps(avg, decision, side)
                if avg is not None and decision else None,
                slippage_vs_arrival_bps=slippage_bps(avg, ref, side)
                if avg is not None and ref else None,
                charges=sum((f.fill.charges for f in got), Decimal(0)),
            ))  # fmt: skip
        return out

    # -- risk ------------------------------------------------------------------------------------

    def risk(self, *, book: str | None, live: LiveView | None = None) -> RiskView:
        today = self.today()
        offline_blocks: dict[str, tuple[EventBlock, ...]] | None = None
        out = []
        for b in [book] if book is not None else self.books:
            switches = [KillSwitchRow(scope=r["scope"], name=r["name"], state=r["state"],
                                      reason=r["reason"], actor=r["actor"],
                                      since=_dt(r["since_ts"]))
                        for r in self.store.query(
                            "SELECT * FROM kill_switches WHERE book_id = ? ORDER BY scope, name",
                            (b,))]  # fmt: skip
            state_rows = self.store.query(
                "SELECT state FROM daily_risk_state WHERE book_id = ? AND ist_date = ?",
                (b, today.isoformat()),
            )
            blocked: Counter[str] = Counter()
            for e in self.store.read(types=[RiskDecision.event_type], book_id=b, ist_date=today):
                decided = e.payload
                if isinstance(decided, RiskDecision) and decided.outcome in (
                    RiskOutcome.REJECTED,
                    RiskOutcome.HALTED,
                ):
                    for reason in decided.reasons:
                        if reason.outcome.value == "block":
                            blocked[reason.code.value] += 1
            daily = json.loads(state_rows[0]["state"]) if state_rows else None
            snapshot = live.risk.get(b) if live is not None else None
            mtm, _, kind = self._valuation(b, live)
            if snapshot is not None:
                usages, sectors = (
                    utilisation(snapshot, self.limits),
                    sector_usage(snapshot, self.limits),
                )
                blocks = snapshot.event_blocks
                equity: Decimal | None = snapshot.equity
            else:
                usages, sectors = self._offline_usage(b, daily, mtm), []
                if offline_blocks is None:
                    offline_blocks = self._event_blocks()
                blocks, equity = offline_blocks, mtm.equity if mtm else None
            out.append(BookRisk(
                book_id=b, kill_switches=switches, daily_state=daily,
                rejections_today=dict(blocked.most_common()), equity=equity, valuation=kind,
                utilisation=[_usage(u) for u in usages], sectors=[_usage(u) for u in sectors],
                event_blocks=sorted((EventBlockRow(instrument_key=k, symbol=symbol_of(k),
                                                   code=x.code.value, start=x.start, end=x.end,
                                                   event_id=x.event_id, reason=x.reason)
                                     for k, xs in blocks.items() for x in xs),
                                    key=lambda r: (r.start, r.symbol)),
            ))  # fmt: skip
        return RiskView(limits_hash=self.limits.limits_hash(),
                        limits=self.limits.model_dump(mode="json"), books=out)  # fmt: skip

    def _offline_usage(self, book: str, daily: dict[str, Any] | None,
                       mtm: MarkToMarket | None) -> list[Usage]:  # fmt: skip
        """Without the engine there are no live marks: only what the stored state and the last
        mark support (no heat, no sectors)."""
        limits = self.limits
        held = self.store.query(
            "SELECT COUNT(*) AS n FROM positions WHERE book_id = ? AND quantity != 0", (book,)
        )[0]["n"]
        out: list[Usage] = []
        if daily is not None:
            sod, peak = Decimal(daily["sod_equity"]), Decimal(daily["peak_equity"])
            last = Decimal(daily["last_equity"])
            out.append(Usage("PF_DAILY_LOSS_MTM", "Daily loss", max(Decimal(0), sod - last),
                             sod * Decimal(str(limits.daily_loss_limit_pct)), "inr"))  # fmt: skip
            drawdown = max(Decimal(0), (peak - last) / peak) if peak > 0 else Decimal(0)
            out.append(Usage("PF_DRAWDOWN", "Drawdown", drawdown,
                             Decimal(str(limits.max_drawdown_pct)), "ratio"))  # fmt: skip
        if mtm is not None:
            out.append(Usage("PF_GROSS", "Gross exposure", mtm.positions_value,
                             mtm.equity * Decimal(str(limits.max_gross_exposure_pct)), "inr"))  # fmt: skip
        out.append(Usage("PF_MAX_POSITIONS", "Positions", Decimal(held),
                         Decimal(limits.max_positions), "count"))  # fmt: skip
        if daily is not None:
            out.append(Usage("PF_ENTRIES_PER_DAY", "Entries today", Decimal(daily.get("entries", 0)),
                             Decimal(limits.max_entries_per_day), "count"))  # fmt: skip
        return out

    def _event_blocks(self) -> dict[str, tuple[EventBlock, ...]]:
        events = [TypedEvent.model_validate_json(r["payload"])
                  for r in self.store.query("SELECT payload FROM typed_events")]  # fmt: skip
        try:
            return event_blocks(events, calendar=self.calendar, rules=EventRules.from_yaml(),
                                now=self.clock.now())  # fmt: skip
        except CalendarCoverageError:
            return {}

    # -- the paired books ------------------------------------------------------------------------

    def books_view(self, live: LiveView | None) -> BooksView:
        capital = self.experiment.capital_inr
        baseline = self.books[0]
        today = self.today()
        rows: dict[str, dict[str, Any]] = {}
        for book in self.books:
            mtm, _, kind = self._valuation(book, live)
            trades = self.store.query("SELECT net_pnl FROM trades WHERE book_id = ?", (book,))
            pnls = [Decimal(r["net_pnl"]) for r in trades]
            spend = sum((Decimal(r["cost_inr"]) for r in self.store.query(
                "SELECT cost_inr FROM llm_calls WHERE book_id = ? AND cost_inr IS NOT NULL",
                (book,))), Decimal(0))  # fmt: skip
            rows[book] = {"mtm": mtm, "kind": kind, "pnls": pnls, "spend": spend}
        base_equity = rows[baseline]["mtm"].equity if rows[baseline]["mtm"] else None
        out = []
        for book, row in rows.items():
            equity = row["mtm"].equity if row["mtm"] else None
            vs = None if book == baseline else baseline
            diff = (equity - base_equity
                    if vs is not None and equity is not None and base_equity is not None
                    else None)  # fmt: skip
            pnls = row["pnls"]
            precision = veto_precision_of(self.store, book, start=date.min, day=today)
            out.append(BookComparison(
                book_id=book, advisor=self.experiment.books[book].advisor, capital=capital,
                equity=equity, valuation=row["kind"],
                return_pct=round(float((equity / capital - 1) * 100), 3) if equity else None,
                realized_net_to_date=sum(pnls, Decimal(0)), closed_trades=len(pnls),
                win_rate=round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
                ai_spend_inr_to_date=row["spend"], vs=vs, equity_difference_inr=diff,
                net_ai_value_inr=diff - row["spend"] if diff is not None else None,
                veto_precision=VetoPrecision.model_validate(precision),
            ))  # fmt: skip
        return BooksView(experiment=self.experiment.experiment, baseline=baseline, books=out)

    # -- AI --------------------------------------------------------------------------------------

    def llm_calls(self, *, role: str | None, book: str | None, day: date | None,
                  limit: int) -> list[LLMCallRow]:  # fmt: skip
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("role", role), ("book_id", book),
                              ("ist_date", day.isoformat() if day else None)):  # fmt: skip
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        rows = self.store.query(f"SELECT * FROM llm_calls{_where(clauses)} ORDER BY seq DESC"
                                " LIMIT ?", [*params, limit])  # fmt: skip
        return [LLMCallRow(
            seq=r["seq"], ts=_dt(r["ts_utc"]), decision_id=r["decision_id"],
            book_id=r["book_id"], role=r["role"], provider=r["provider"], model=r["model"],
            attempt=r["attempt"], prompt_version=r["prompt_version"], outcome=r["outcome"],
            tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], latency_ms=r["latency_ms"],
            cost_usd=_dec(r["cost_usd"]), cost_inr=_dec(r["cost_inr"]),
            cache_hit=bool(r["cache_hit"]),
        ) for r in rows]  # fmt: skip

    def spend(self, *, group_by: GroupBy, since: date | None, until: date | None) -> SpendView:
        column = {"role": "role", "book": "book_id", "day": "ist_date", "model": "model"}[group_by]
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ist_date >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ist_date <= ?")
            params.append(until.isoformat())
        totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal(0),
            "cost_inr": Decimal(0)})  # fmt: skip
        for r in self.store.query(
            f"SELECT {column} AS k, provider, tokens_in, tokens_out, cost_usd, cost_inr"
            f" FROM llm_calls{_where(clauses)}", params,
        ):  # fmt: skip
            key = str(r["k"]) if r["k"] is not None else "-"
            if group_by == "model":
                key = f"{r['provider']}:{key}"
            t = totals[key]
            t["calls"] += 1
            t["tokens_in"] += r["tokens_in"]
            t["tokens_out"] += r["tokens_out"]
            t["cost_usd"] += _dec(r["cost_usd"]) or Decimal(0)
            t["cost_inr"] += _dec(r["cost_inr"]) or Decimal(0)
        rows = [SpendRow(key=k, **v) for k, v in sorted(totals.items())]
        return SpendView(group_by=group_by, rows=rows,
                         total_inr=sum((r.cost_inr for r in rows), Decimal(0)))  # fmt: skip

    def models(self) -> list[RoleModels]:
        today = self.today().isoformat()
        out = []
        for role, config in sorted(role_configs(self.settings).items()):
            health = []
            for spec in config.chain:
                calls = self.store.query(
                    "SELECT outcome, latency_ms, ts_utc FROM llm_calls WHERE provider = ?"
                    " AND model = ? AND role = ? AND ist_date = ? ORDER BY seq",
                    (spec.provider, spec.model, role, today),
                )  # fmt: skip
                last = calls[-1] if calls else None
                health.append(ModelHealth(
                    model=spec.spec, calls_today=len(calls),
                    errors_today=sum(1 for c in calls if c["outcome"] in _ERRORS),
                    p50_latency_ms=_pct([c["latency_ms"] for c in calls], 0.5),
                    p95_latency_ms=_pct([c["latency_ms"] for c in calls], 0.95),
                    last_outcome=last["outcome"] if last else None,
                    last_call=_dt(last["ts_utc"]) if last else None,
                ))  # fmt: skip
            out.append(RoleModels(role=role, enabled=config.enabled,
                                  chain=[s.spec for s in config.chain], models=health))  # fmt: skip
        return out

    def decision_models(self, *, day: date | None) -> list[DecisionModelStats]:
        clauses, params = ([], []) if day is None else (["ist_date = ?"], [day.isoformat()])
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in self.store.query(f"SELECT * FROM decision_model_calls{_where(clauses)}", params):
            groups[(r["task"], r["model"], r["checkpoint"])].append(r)
        out = []
        for (task, model, checkpoint), calls in sorted(groups.items()):
            live = [c for c in calls if not c["shadow"]]
            latencies = [c["latency_ms"] for c in live]
            out.append(DecisionModelStats(
                task=task, model=model, checkpoint=checkpoint, calls=len(live),
                p50_latency_ms=_pct(latencies, 0.5), p95_latency_ms=_pct(latencies, 0.95),
                escalation_rate=round(sum(c["escalated"] for c in live) / len(live), 4)
                if live else None,
                calibrated_rate=round(sum(c["calibrated"] for c in live) / len(live), 4)
                if live else None,
                shadow_calls=len(calls) - len(live),
                outcomes=dict(Counter(c["outcome"] for c in calls)),
            ))  # fmt: skip
        return out

    # -- events, reports, system, config ------------------------------------------------------------

    def typed_events(self, *, symbol: str | None, day: date | None,
                     limit: int) -> list[TypedEventRow]:  # fmt: skip
        out = []
        for r in self.store.query("SELECT * FROM typed_events ORDER BY published_at DESC"):
            if symbol is not None and symbol_of(r["instrument_key"]) != symbol:
                continue
            published = _dt(r["published_at"])
            if day is not None and published.astimezone(IST).date() != day:
                continue
            out.append(TypedEventRow(
                event_id=r["event_id"], instrument_key=r["instrument_key"],
                symbol=symbol_of(r["instrument_key"]), published_at=published,
                relevant=bool(r["relevant"]), announcement_type=r["announcement_type"],
                direction=r["direction"], materiality=r["materiality"],
                data=json.loads(r["payload"]),
            ))  # fmt: skip
            if len(out) >= limit:
                break
        return out

    def tape_bars(self, instrument_key: str, *, adjusted: bool) -> list[Bar]:
        """The instrument's daily bars from the newest taped history (no engine running)."""
        root = self.tape_dir
        if not root.is_dir():
            return []
        for day_dir in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            bars = [b for b in read_bars(root, day) if b.instrument_key == instrument_key]
            if bars:
                wanted = [b for b in bars if b.adjusted == adjusted]
                return sorted(wanted or bars, key=lambda b: b.session_date)
        return []

    def report(self, day: date) -> dict[str, Any] | None:
        path = self.reports_dir / f"{day.isoformat()}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def system(self, live: LiveView | None, *, running: bool) -> SystemView:
        beat = self._latest(Heartbeat.event_type)
        lag = self._latest("LoopLag")
        started = self._latest(ProcessStarted.event_type)
        path = self.store.path
        size = sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal"))
                   if p.exists())  # fmt: skip
        reconciles = []
        for r in self.store.query(
            "SELECT MAX(seq) AS seq FROM events WHERE type = ?"
            " GROUP BY book_id, json_extract(payload, '$.scope')",
            (ReconciliationResult.event_type,),
        ):  # fmt: skip
            found = self.store.read(since_seq=int(r["seq"]) - 1, limit=1)
            if found and isinstance(p := found[0].payload, ReconciliationResult):
                reconciles.append(ReconcileRow(book_id=p.book_id, scope=p.scope,
                                               in_sync=p.in_sync, diffs=list(p.diffs),
                                               ts=found[0].ts_utc))  # fmt: skip
        hb = beat.payload if beat is not None and isinstance(beat.payload, Heartbeat) else None
        versions = {"python": platform.python_version(), "app": _app_version()}
        if started is not None and isinstance(started.payload, ProcessStarted):
            versions["build"] = started.payload.version
        return SystemView(
            environment=self.settings.environment, running=running, pid=os.getpid(),
            tasks=list(live.tasks) if live else [], uptime_s=hb.uptime_s if hb else None,
            loop_lag_ms=hb.loop_lag_ms if hb else None,
            last_heartbeat=beat.ts_utc if beat else None,
            last_loop_lag=lag.ts_utc if lag else None,
            quote_age_s=dict(live.quote_age_s) if live else {}, store_bytes=size,
            store_path=path.name, last_seq=self.store.last_seq(),
            schema_version=len(load_migrations()),
            last_reconcile=sorted(reconciles, key=lambda r: (r.book_id, r.scope)),
            versions=versions,
            prompts={name: t.prompt_version for name, t in sorted(_PROMPTS.items())},
        )  # fmt: skip

    def config(self) -> ConfigView:
        s = self.settings
        requested = str(s.execution_mode)
        note = None if requested in ("local_paper", "shadow") else (
            f"EXECUTION_MODE={requested} is ignored: the v2 engine trades on the simulated "
            "broker only")  # fmt: skip
        e = self.experiment
        return ConfigView(
            environment=s.environment, execution_mode_requested=requested,
            execution_mode_note=note, market_data_source=str(s.market_data_source),
            experiment={
                "experiment": e.experiment, "capital_inr": str(e.capital_inr),
                "entry_window": e.entry_window, "product": e.product,
                "strategies": e.strategies.model_dump(mode="json"),
                "books": {b: spec.model_dump(mode="json") for b, spec in e.books.items()},
                "learning_injection": e.learning_injection,
            },
            llm_roles={r: [m.spec for m in c.chain]
                       for r, c in sorted(role_configs(s).items())},
            decision_models={"laya_enabled": bool(s.decision_laya_enabled),
                             "laya_checkpoint": s.decision_laya_checkpoint,
                             "jev_configured": s.typesafe_api_key is not None},
            announcements_enabled=bool(s.announcements_enabled),
            telegram_configured=bool(s.telegram_enabled and s.telegram_bot_token
                                     and s.telegram_chat_id),
            limits_hash=self.limits.limits_hash(), read_only=self.read_only,
            llm_budget_daily_inr=Decimal(str(s.llm_budget_daily_inr)),
        )  # fmt: skip

    # -- screens (plan M10.4) ------------------------------------------------------------------

    def markers(self, instrument_key: str) -> list[BarMarker]:
        """Fills (entries/exits) and announcements of one instrument, for its chart."""
        out = [BarMarker(date=_dt(r["ts_utc"]).astimezone(IST).date(),
                         kind="entry" if r["side"] == "BUY" else "exit",
                         text=f"{r['book_id']} {r['side']} {r['quantity']} @ {r['price']}")
               for r in self.store.query("SELECT * FROM fills WHERE instrument_key = ? ORDER BY seq",
                                         (instrument_key,))]  # fmt: skip
        for r in self.store.query(
            "SELECT published_at, announcement_type, direction FROM typed_events"
            " WHERE instrument_key = ? AND relevant = 1 ORDER BY published_at", (instrument_key,)
        ):  # fmt: skip
            label = " ".join(x for x in (r["announcement_type"], r["direction"]) if x)
            out.append(BarMarker(date=_dt(r["published_at"]).astimezone(IST).date(), kind="event",
                                 text=label or "announcement"))  # fmt: skip
        return sorted(out, key=lambda m: m.date)

    def alerts(self, *, level: str | None, day: date | None, limit: int) -> list[AlertRow]:
        out: list[AlertRow] = []
        for e in reversed(self.store.read(types=[Alert.event_type], ist_date=day)):
            p = e.payload
            if not isinstance(p, Alert) or (level is not None and p.level != level):
                continue
            out.append(AlertRow(seq=e.seq or 0, ts=e.ts_utc, level=p.level, key=p.key,
                                message=redact(p.message), book_id=e.book_id))  # fmt: skip
            if len(out) >= limit:
                break
        return out

    def equity(self, live: LiveView | None) -> EquityView:
        capital = self.experiment.capital_inr
        series = []
        first: date | None = None
        for book in self.books:
            daily: dict[date, Decimal] = {}
            for e in self.store.read(types=[MarkToMarket.event_type], book_id=book):
                if isinstance(e.payload, MarkToMarket):
                    daily[e.ist_date] = e.payload.equity  # the last mark of each day
            if live is not None and book in live.valuations:
                daily[self.today()] = live.valuations[book].equity
            peak, points = capital, []
            for day in sorted(daily):
                value = daily[day]
                peak = max(peak, value)
                points.append(EquityPoint(
                    date=day, equity=value, peak=peak,
                    return_pct=round(float((value / capital - 1) * 100), 3),
                    drawdown_pct=round(float((peak - value) / peak * 100), 3) if peak else 0.0,
                ))  # fmt: skip
            if points and (first is None or points[0].date < first):
                first = points[0].date
            series.append(EquitySeries(book_id=book, points=points))
        closes = dict(live.index_closes) if live is not None and live.index_closes else {
            b.session_date: b.close for b in self.tape_bars(INDEX_KEY, adjusted=False)
        }  # fmt: skip
        bench: list[BenchmarkPoint] = []
        if first is not None and closes:
            before = [d for d in closes if d < first]
            base_day = max(before) if before else min(closes)  # the session before the first
            base = closes[base_day]
            bench = [BenchmarkPoint(date=d, close=c, return_pct=round((c / base - 1) * 100, 3))
                     for d, c in sorted(closes.items()) if d >= base_day]  # fmt: skip
        return EquityView(capital=capital, books=series, benchmark=self.experiment.benchmark,
                          benchmark_points=bench)  # fmt: skip

    def watchlist(self, live: LiveView | None) -> list[WatchRow]:
        today = self.today()
        if live is not None and live.instruments:
            instruments = dict(live.instruments)
            quotes = dict(live.quotes)
        else:  # the newest taped quotes
            quotes = self._tape_quotes()
            instruments = {}
        keys = sorted(set(instruments) | set(quotes))
        signals: dict[str, list[str]] = defaultdict(list)
        for e in self.store.read(types=[SignalGenerated.event_type], ist_date=today):
            if isinstance(e.payload, SignalGenerated):
                sig = e.payload.signal
                signals[sig.instrument_key].append(f"{sig.strategy} {sig.side.value}")
        recent: dict[str, list[str]] = defaultdict(list)
        for r in self.store.query(
            "SELECT instrument_key, announcement_type FROM typed_events WHERE relevant = 1"
            " ORDER BY published_at DESC LIMIT 500"
        ):  # fmt: skip
            label = r["announcement_type"] or "announcement"
            if label not in recent[r["instrument_key"]]:
                recent[r["instrument_key"]].append(label)
        held = {r["instrument_key"] for r in self.store.query(
            "SELECT instrument_key FROM positions WHERE quantity != 0")}  # fmt: skip
        now = self.clock.now()
        out = []
        for key in keys:
            if key == INDEX_KEY:
                continue
            q = quotes.get(key)
            inst = instruments.get(key)
            out.append(WatchRow(
                instrument_key=key, symbol=symbol_of(key), sector=inst.sector if inst else None,
                ltp=q.ltp if q else None, prev_close=q.prev_close if q else None,
                change_pct=round((q.ltp / q.prev_close - 1) * 100, 3)
                if q and q.prev_close else None,
                quote_age_s=round(q.age_seconds(now), 1) if q else None,
                source=q.source.value if q else None, signals_today=sorted(signals.get(key, [])),
                events=recent.get(key, [])[:3], held=key in held,
            ))  # fmt: skip
        return out

    def _tape_quotes(self) -> dict[str, Quote]:
        root = self.tape_dir
        if not root.is_dir():
            return {}
        for day_dir in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            latest: dict[str, Quote] = {}
            for q in read_quotes(root, day):
                latest[q.instrument_key] = q
            if latest:
                return latest
        return {}

    def reports_list(self, *, limit: int) -> list[ReportSummary]:
        if not self.reports_dir.is_dir():
            return []
        out = []
        for path in sorted(self.reports_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                day = date.fromisoformat(str(data["date"]))
            except (ValueError, KeyError, OSError):
                continue
            books = {}
            for b, section in dict(data.get("books", {})).items():
                comparison = dict(data.get("comparison", {})).get(b, {})
                books[b] = ReportBook(
                    end_equity=section.get("capital", {}).get("end_equity"),
                    day_return_pct=section.get("pnl", {}).get("day_return_pct"),
                    cumulative_return_pct=section.get("pnl", {}).get("cumulative_return_pct"),
                    net_ai_value_inr=comparison.get("net_ai_value_inr"),
                    exits=section.get("trades", {}).get("exits"),
                )  # fmt: skip
            out.append(ReportSummary(date=day, experiment=str(data.get("experiment", "")),
                                     books=books))  # fmt: skip
        return out

    def report_markdown(self, day: date) -> Document | None:
        path = self.reports_dir / f"{day.isoformat()}.md"
        if not path.is_file():
            return None
        return Document(title=f"Daily report {day.isoformat()}",
                        markdown=path.read_text(encoding="utf-8"))  # fmt: skip

    def preregistration(self) -> Document | None:
        if not PREREGISTRATION.is_file():
            return None
        text = PREREGISTRATION.read_text(encoding="utf-8")
        title = next((line.lstrip("# ").strip() for line in text.splitlines()
                      if line.startswith("# ")), PREREGISTRATION.name)  # fmt: skip
        return Document(title=title, markdown=text)

    def logs(self, *, level: str | None, contains: str | None, limit: int) -> list[LogLine]:
        """The newest lines of today's (else the latest) log file, newest first, redacted."""
        logs_dir = Path(self.settings.logs_dir)
        files = (
            sorted(logs_dir.glob("rakshaquant-*.log"), reverse=True) if logs_dir.is_dir() else []
        )
        if not files:
            return []
        lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
        out: list[LogLine] = []
        needle = contains.lower() if contains else None
        for raw in reversed(lines):
            try:
                rec = json.loads(raw)
                line = LogLine(ts=datetime.fromisoformat(rec["ts"]), level=str(rec["level"]),
                               logger=str(rec.get("logger", "")),
                               message=redact(str(rec.get("msg", ""))),
                               decision_id=rec.get("decision_id"))  # fmt: skip
            except (ValueError, KeyError, TypeError):
                continue
            if level is not None and line.level != level:
                continue
            if needle and needle not in line.message.lower() and needle not in line.logger.lower():
                continue
            out.append(line)
            if len(out) >= limit:
                break
        return out

    def calibration(self) -> CalibrationView:
        path = Path(self.settings.models_dir) / "calibration.json"
        if not path.is_file():
            return CalibrationView(fitted=False, temperatures={}, meta={})
        data = json.loads(path.read_text(encoding="utf-8"))
        temperatures = CalibrationMap.load(path).temperatures
        return CalibrationView(fitted=bool(temperatures), temperatures=temperatures,
                               meta=dict(data.get("meta", {})))  # fmt: skip


def _usage(u: Usage) -> Utilisation:
    return Utilisation(key=u.key, label=u.label, used=u.used, limit=u.limit, unit=u.unit,
                       fraction=round(u.fraction, 4) if u.fraction is not None else None)  # fmt: skip


def _where(clauses: Sequence[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _app_version() -> str:
    try:
        return importlib.metadata.version("trading-agent")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
