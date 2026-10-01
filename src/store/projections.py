"""
Projections: read-model tables maintained from events (plan M1.5).

A projector owns some tables and handles some event types. It runs inside the transaction that
appends the event, and it must be a pure function of the stored event (payload, ``seq``,
``ts_utc``), so that :meth:`EventStore.rebuild_projections` reproduces the incrementally
maintained tables exactly. Projections are never written to directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC
from decimal import Decimal
from typing import Protocol

from src.domain import events as ev
from src.domain.base import EventPayload
from src.domain.events import Event
from src.domain.types import OrderStatus, RiskDecision, TypedEvent


class Projector(Protocol):
    tables: tuple[str, ...]
    handles: frozenset[str]

    def apply(self, conn: sqlite3.Connection, event: Event) -> None: ...


def _seq(event: Event) -> int:
    if event.seq is None:
        raise ValueError("projectors only see stored events (seq is None)")
    return event.seq


def _ts(event: Event) -> str:
    return event.ts_utc.isoformat()


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _types(*classes: type[EventPayload]) -> frozenset[str]:
    return frozenset(cls.event_type for cls in classes)


class OrdersProjector:
    tables: tuple[str, ...] = ("orders",)
    handles: frozenset[str] = _types(
        ev.OrderSubmitted,
        ev.OrderAcked,
        ev.OrderRejected,
        ev.OrderUnknown,
        ev.OrderCancelled,
        ev.OrderExpired,
        ev.FillReceived,
    )

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p, seq, ts = event.payload, _seq(event), _ts(event)
        if isinstance(p, ev.OrderSubmitted):
            o, i = p.order, p.order.intent
            conn.execute(
                "INSERT OR REPLACE INTO orders (client_order_id, book_id, decision_id, intent_id,"
                " instrument_key, strategy, side, product, order_type, kind, reason, quantity,"
                " status, filled_qty, avg_fill_price, broker_order_id, last_message, created_seq,"
                " updated_seq, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    o.client_order_id,
                    i.book_id,
                    i.decision_id,
                    i.intent_id,
                    i.instrument.key,
                    i.strategy,
                    i.side.value,
                    i.product.value,
                    i.order_type.value,
                    i.kind.value,
                    i.reason.value,
                    o.quantity,
                    o.status.value,
                    o.filled_qty,
                    _dec(o.avg_fill_price),
                    o.broker_order_id,
                    None,
                    seq,
                    seq,
                    ts,
                ),
            )
        elif isinstance(p, ev.OrderAcked):
            self._update(
                conn,
                p.client_order_id,
                seq,
                ts,
                status=p.status.value,
                broker_order_id=p.broker_order_id,
            )
        elif isinstance(p, ev.OrderRejected):
            message = f"{p.reason}: {p.message}" if p.message else p.reason
            self._update(
                conn,
                p.client_order_id,
                seq,
                ts,
                status=OrderStatus.REJECTED.value,
                last_message=message,
            )
        elif isinstance(p, ev.OrderUnknown):
            self._update(
                conn,
                p.client_order_id,
                seq,
                ts,
                status=OrderStatus.UNKNOWN.value,
                last_message=p.error or None,
            )
        elif isinstance(p, ev.OrderCancelled):
            self._update(
                conn,
                p.client_order_id,
                seq,
                ts,
                status=OrderStatus.CANCELLED.value,
                filled_qty=p.filled_qty,
                last_message=p.reason or None,
            )
        elif isinstance(p, ev.OrderExpired):
            self._update(
                conn,
                p.client_order_id,
                seq,
                ts,
                status=OrderStatus.EXPIRED.value,
                filled_qty=p.filled_qty,
            )
        elif isinstance(p, ev.FillReceived):
            self._update(
                conn,
                p.fill.client_order_id,
                seq,
                ts,
                status=p.order_status.value,
                filled_qty=p.order_filled_qty,
                avg_fill_price=_dec(p.order_avg_price),
            )

    @staticmethod
    def _update(
        conn: sqlite3.Connection,
        client_order_id: str,
        seq: int,
        ts: str,
        **columns: str | int | None,
    ) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        conn.execute(
            f"UPDATE orders SET {assignments}, updated_seq = ?, updated_ts = ?"
            " WHERE client_order_id = ?",
            (*columns.values(), seq, ts, client_order_id),
        )


class FillsProjector:
    tables: tuple[str, ...] = ("fills",)
    handles: frozenset[str] = _types(ev.FillReceived)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.FillReceived):
            return
        f = p.fill
        # Fills are de-duplicated by fill_id: a replayed broker fill never counts twice.
        conn.execute(
            "INSERT OR IGNORE INTO fills (fill_id, client_order_id, book_id, decision_id,"
            " instrument_key, side, quantity, price, charges, ts_utc, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f.fill_id,
                f.client_order_id,
                f.book_id,
                f.decision_id,
                f.instrument_key,
                f.side.value,
                f.quantity,
                str(f.price),
                str(f.charges),
                f.ts.astimezone(UTC).isoformat(),
                _seq(event),
            ),
        )


class PositionsProjector:
    tables: tuple[str, ...] = ("positions",)
    handles: frozenset[str] = _types(ev.PositionChanged)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.PositionChanged):
            return
        pos = p.position
        conn.execute(
            "INSERT OR REPLACE INTO positions (book_id, instrument_key, product, quantity,"
            " avg_price, realized_pnl, updated_ts, updated_seq) VALUES (?,?,?,?,?,?,?,?)",
            (
                pos.book_id,
                pos.instrument_key,
                pos.product.value,
                pos.quantity,
                _dec(pos.avg_price),
                str(pos.realized_pnl),
                _ts(event),
                _seq(event),
            ),
        )


class TradesProjector:
    tables: tuple[str, ...] = ("trades",)
    handles: frozenset[str] = _types(ev.TradeClosed)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.TradeClosed):
            return
        conn.execute(
            "INSERT OR REPLACE INTO trades (trade_id, book_id, decision_id, exit_decision_id,"
            " instrument_key, strategy, side, quantity, entry_price, exit_price, entry_ts,"
            " exit_ts, gross_pnl, charges, net_pnl, exit_reason, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.trade_id,
                p.book_id,
                p.decision_id,
                p.exit_decision_id,
                p.instrument_key,
                p.strategy,
                p.side.value,
                p.quantity,
                str(p.entry_price),
                str(p.exit_price),
                p.entry_ts.isoformat(),
                p.exit_ts.isoformat(),
                str(p.gross_pnl),
                str(p.charges),
                str(p.net_pnl),
                p.exit_reason,
                _seq(event),
            ),
        )


class DailyRiskStateProjector:
    tables: tuple[str, ...] = ("daily_risk_state",)
    handles: frozenset[str] = _types(ev.DailyRiskStateRolled)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.DailyRiskStateRolled):
            return
        conn.execute(
            "INSERT OR REPLACE INTO daily_risk_state (book_id, ist_date, state, updated_seq)"
            " VALUES (?,?,?,?)",
            (p.book_id, p.ist_date.isoformat(), json.dumps(p.state, sort_keys=True), _seq(event)),
        )


class KillSwitchesProjector:
    tables: tuple[str, ...] = ("kill_switches",)
    handles: frozenset[str] = _types(ev.KillSwitchChanged)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.KillSwitchChanged):
            return
        conn.execute(
            "INSERT OR REPLACE INTO kill_switches (book_id, scope, name, state, reason, actor,"
            " since_ts, updated_seq) VALUES (?,?,?,?,?,?,?,?)",
            (
                p.book_id,
                p.scope.value,
                p.name,
                p.current.value,
                p.reason,
                p.actor,
                _ts(event),
                _seq(event),
            ),
        )


class DecisionsProjector:
    tables: tuple[str, ...] = ("decisions",)
    handles: frozenset[str] = _types(RiskDecision)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, RiskDecision):
            return
        conn.execute(
            "INSERT OR REPLACE INTO decisions (book_id, intent_id, decision_id, instrument_key,"
            " strategy, side, kind, outcome, qty_approved, limits_hash, payload, ts_utc, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.book_id,
                p.intent_id,
                p.decision_id,
                p.instrument_key,
                p.strategy,
                p.side.value,
                p.kind.value,
                p.outcome.value,
                p.qty_approved,
                p.limits_hash,
                ev.payload_json(p),
                _ts(event),
                _seq(event),
            ),
        )


class LLMCallsProjector:
    tables: tuple[str, ...] = ("llm_calls",)
    handles: frozenset[str] = _types(ev.LLMCall)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.LLMCall):
            return
        conn.execute(
            "INSERT OR REPLACE INTO llm_calls (seq, ts_utc, ist_date, decision_id, book_id, role,"
            " provider, model, attempt, prompt_version, outcome, tokens_in, tokens_out,"
            " latency_ms, cost_usd, cost_inr, cache_hit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _seq(event),
                _ts(event),
                event.ist_date.isoformat(),
                p.decision_id,
                p.book_id,
                p.role,
                p.provider,
                p.model,
                p.attempt,
                p.prompt_version,
                p.outcome.value,
                p.tokens_in,
                p.tokens_out,
                p.latency_ms,
                _dec(p.cost_usd),
                _dec(p.cost_inr),
                int(p.cache_hit),
            ),
        )


class DecisionModelCallsProjector:
    tables: tuple[str, ...] = ("decision_model_calls",)
    handles: frozenset[str] = _types(ev.DecisionModelCall)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, ev.DecisionModelCall):
            return
        conn.execute(
            "INSERT OR REPLACE INTO decision_model_calls (seq, ts_utc, ist_date, decision_id,"
            " book_id, task, model, checkpoint, latency_ms, escalated, shadow, calibrated,"
            " outcome, answers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _seq(event),
                _ts(event),
                event.ist_date.isoformat(),
                p.decision_id,
                p.book_id,
                p.task,
                p.model,
                p.checkpoint,
                p.latency_ms,
                int(p.escalated),
                int(p.shadow),
                int(p.calibrated),
                p.outcome.value,
                json.dumps(p.answers, sort_keys=True),
            ),
        )


class TypedEventsProjector:
    tables: tuple[str, ...] = ("typed_events",)
    handles: frozenset[str] = _types(TypedEvent)

    def apply(self, conn: sqlite3.Connection, event: Event) -> None:
        p = event.payload
        if not isinstance(p, TypedEvent):
            return
        conn.execute(
            "INSERT OR REPLACE INTO typed_events (event_id, instrument_key, published_at,"
            " relevant, announcement_type, direction, materiality, payload, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                p.event_id,
                p.instrument_key,
                p.published_at.isoformat(),
                int(p.relevant),
                p.announcement_type.value if p.announcement_type else None,
                p.direction.value if p.direction else None,
                p.materiality.value if p.materiality else None,
                ev.payload_json(p),
                _seq(event),
            ),
        )


DEFAULT_PROJECTORS: tuple[Projector, ...] = (
    OrdersProjector(),
    FillsProjector(),
    PositionsProjector(),
    TradesProjector(),
    DailyRiskStateProjector(),
    KillSwitchesProjector(),
    DecisionsProjector(),
    LLMCallsProjector(),
    DecisionModelCallsProjector(),
    TypedEventsProjector(),
)
