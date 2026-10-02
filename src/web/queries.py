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
from src.domain.calendar import NSECalendar
from src.domain.clock import Clock, now_ist
from src.domain.events import (
    Event,
    Heartbeat,
    LLMOutcome,
    MarkToMarket,
    ProcessStarted,
    ReconciliationResult,
    SessionStateChanged,
    SignalDisposition,
    payload_json,
)
from src.domain.types import Bar, KillScope, OrderStatus, RiskDecision, RiskOutcome
from src.evaluation.daily_report import veto_precision_of
from src.evaluation.experiment import ExperimentConfig
from src.llm.registry import role_configs
from src.store.event_store import EventStore
from src.store.sqlite import load_migrations
from src.store.tape import read_bars
from src.utils.market_time import IST
from src.web.models import (
    BookComparison,
    BookRisk,
    BookSummary,
    BooksView,
    ConfigView,
    DecisionModelStats,
    DecisionRow,
    FillRow,
    KillSwitchRow,
    Lineage,
    LineageEvent,
    LLMCallRow,
    ModelHealth,
    OrderRow,
    PositionRow,
    ReconcileRow,
    RiskView,
    RoleModels,
    SessionInfo,
    SpendRow,
    SpendView,
    Summary,
    SystemView,
    TradeRow,
    TypedEventRow,
    VetoPrecision,
)

Valuation = Literal["live", "last_mark", "none"]
GroupBy = Literal["role", "book", "day", "model"]
_TERMINAL = tuple(s.value for s in OrderStatus if s.is_terminal)
_ERRORS = tuple(o.value for o in LLMOutcome if o is not LLMOutcome.OK)
MAX_ROWS = 1000


@dataclass(frozen=True)
class LiveView:
    """The running engine's in-memory state, captured on the event loop."""

    valuations: Mapping[str, MarkToMarket] = field(default_factory=dict)
    marks: Mapping[str, Decimal] = field(default_factory=dict)
    tasks: tuple[str, ...] = ()
    quote_age_s: Mapping[str, float] = field(default_factory=dict)


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
            ))  # fmt: skip
        return Summary(
            environment=self.settings.environment, demo=self.demo, running=running,
            experiment=self.experiment.experiment, session=session,
            market_open=self.calendar.is_market_open(self.clock.now()), now=self.clock.now(),
            last_seq=self.store.last_seq(), books=books,
        )  # fmt: skip

    # -- blotter ---------------------------------------------------------------------------------

    def positions(self, live: LiveView | None, *, book: str | None) -> list[PositionRow]:
        sql, params = "SELECT * FROM positions WHERE quantity != 0", []
        if book is not None:
            sql, params = sql + " AND book_id = ?", [book]
        marks = live.marks if live is not None else {}
        out = []
        for r in self.store.query(sql + " ORDER BY book_id, instrument_key", params):
            avg, mark = _dec(r["avg_price"]), marks.get(r["instrument_key"])
            unrealized = (
                (mark - avg) * r["quantity"] if mark is not None and avg is not None else None
            )
            out.append(PositionRow(
                book_id=r["book_id"], instrument_key=r["instrument_key"],
                symbol=symbol_of(r["instrument_key"]), product=r["product"],
                quantity=r["quantity"], avg_price=avg, realized_pnl=Decimal(r["realized_pnl"]),
                mark=mark, unrealized_pnl=unrealized, updated_ts=_dt(r["updated_ts"]),
            ))  # fmt: skip
        return out

    def orders(self, *, book: str | None, status: str | None, day: date | None,
               limit: int) -> list[OrderRow]:  # fmt: skip
        clauses, params = self._filters(book=book, day=day, alias="o")
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

    def fills(self, *, book: str | None, day: date | None, limit: int) -> list[FillRow]:
        clauses, params = self._filters(book=book, day=day, alias="f")
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
               limit: int) -> list[TradeRow]:  # fmt: skip
        clauses, params = self._filters(book=book, day=day, alias="t")
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
    def _filters(*, book: str | None, day: date | None, alias: str) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if book is not None:
            clauses.append(f"{alias}.book_id = ?")
            params.append(book)
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
        return Lineage(decision_id=decision_id, exit_decision_ids=exits, events=[
            LineageEvent(seq=e.seq or 0, ts=e.ts_utc, type=e.type, decision_id=e.decision_id,
                         book_id=e.book_id, source=e.source,
                         data=json.loads(payload_json(e.payload)))
            for e in events
        ])  # fmt: skip

    # -- risk ------------------------------------------------------------------------------------

    def risk(self, *, book: str | None) -> RiskView:
        today = self.today()
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
            out.append(BookRisk(
                book_id=b, kill_switches=switches,
                daily_state=json.loads(state_rows[0]["state"]) if state_rows else None,
                rejections_today=dict(blocked.most_common()),
            ))  # fmt: skip
        return RiskView(limits_hash=self.limits.limits_hash(),
                        limits=self.limits.model_dump(mode="json"), books=out)  # fmt: skip

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
        root = Path(self.settings.tape_dir)
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
        )  # fmt: skip


def _where(clauses: Sequence[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _app_version() -> str:
    try:
        return importlib.metadata.version("trading-agent")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
