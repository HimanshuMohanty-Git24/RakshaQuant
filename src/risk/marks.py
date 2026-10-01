"""Marking the book for risk: per-position owners and open P&L by strategy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from src.domain.types import Position
from src.oms.position_book import PositionBook


def open_positions(book: PositionBook, now: datetime) -> list[Position]:
    return [p for p in book.positions(now) if p.quantity]


def owner(book: PositionBook, position: Position) -> str:
    """The strategy of the position's oldest open lot (FIFO closes it first)."""
    lots = book.lots(position.instrument_key, position.product)
    return lots[0].strategy if lots else "unknown"


def unrealized_by_strategy(
    book: PositionBook, marks: Mapping[str, Decimal], now: datetime
) -> dict[str, Decimal]:
    """Open P&L per strategy, lot by lot, at ``marks`` (every open position needs a mark)."""
    out: dict[str, Decimal] = {}
    for position in open_positions(book, now):
        sign = 1 if position.quantity > 0 else -1
        mark = marks[position.instrument_key]
        for lot in book.lots(position.instrument_key, position.product):
            pnl = (mark - lot.price) * lot.quantity * sign
            out[lot.strategy] = out.get(lot.strategy, Decimal(0)) + pnl
    return out
