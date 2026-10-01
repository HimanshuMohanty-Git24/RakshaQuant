"""
The PositionBook (plan M3.4; audit §F.1 "one book", F-01/F-02/F-07): a book's positions and cash,
changed **only by fills**, each fill applied at most once (by ``fill_id``).

* One **net** position per (instrument, product): two buys of 10 then a sell of 20 is flat, never
  "long 10 and short 10".
* Lots are closed **FIFO**. Realized P&L is **net of both legs' charges**: each lot carries its
  unallocated entry charges, and a closing fill's charges are split pro rata over what it closes.
* A fill larger than the position closes it and opens the opposite side with the rest (the OMS's
  reduce-only rule means exits never do this).
* Identity: ``equity = cash + sum(quantity x mark)``, and ``equity - starting_cash`` equals the
  realized P&L plus, for open lots, ``(mark - price) x quantity - unallocated charges``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.domain.types import Fill, Position, Product, Side

_ALLOC_QUANTUM = Decimal("0.0001")  # charge allocation granularity: 1/100 paisa


@dataclass
class Lot:
    quantity: int  # always positive; the side is the position's
    price: Decimal
    charges: Decimal  # entry charges not yet allocated to a closed trade
    opened_at: datetime
    decision_id: str
    strategy: str
    fill_id: str


@dataclass(frozen=True)
class ClosedPiece:
    """One FIFO lot (or part of one) closed by a fill: becomes a ``TradeClosed`` event."""

    trade_id: str
    instrument_key: str
    product: Product
    side: Side  # side of the opening leg
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    entry_ts: datetime
    exit_ts: datetime
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    decision_id: str
    exit_decision_id: str
    strategy: str
    exit_reason: str


@dataclass(frozen=True)
class FillOutcome:
    position: Position
    closed: tuple[ClosedPiece, ...] = ()
    duplicate: bool = False


@dataclass
class _Book:
    lots: deque[Lot] = field(default_factory=deque)
    direction: int = 0  # +1 long, -1 short, 0 flat
    realized: Decimal = Decimal(0)
    updated_at: datetime | None = None

    @property
    def quantity(self) -> int:
        return self.direction * sum(lot.quantity for lot in self.lots)


class PositionBook:
    def __init__(self, book_id: str, starting_cash: Decimal) -> None:
        self.book_id = book_id
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self._books: dict[tuple[str, Product], _Book] = {}
        self._seen: set[str] = set()

    # -- the only mutator ------------------------------------------------------------------------

    def apply(
        self, fill: Fill, *, product: Product, strategy: str, exit_reason: str = ""
    ) -> FillOutcome:
        if fill.book_id != self.book_id:
            raise ValueError(f"fill for book {fill.book_id} applied to book {self.book_id}")
        key = (fill.instrument_key, product)
        book = self._books.setdefault(key, _Book())
        if fill.fill_id in self._seen:
            return FillOutcome(self._position(key, book, fill.ts), duplicate=True)
        self._seen.add(fill.fill_id)

        notional = fill.price * fill.quantity
        if fill.side is Side.BUY:
            self.cash -= notional + fill.charges
        else:
            self.cash += notional - fill.charges

        sign = fill.side.sign
        closed: list[ClosedPiece] = []
        remaining, exit_charges = fill.quantity, fill.charges
        if book.direction not in (0, sign):  # reduce or close, FIFO
            while remaining and book.lots:
                lot = book.lots[0]
                take = min(remaining, lot.quantity)
                # Pro rata, but the last piece of a lot / of the fill takes the exact remainder,
                # so allocations always sum to the charges (no rounding residue in the ledger).
                entry_alloc = (
                    lot.charges
                    if take == lot.quantity
                    else (lot.charges * take / lot.quantity).quantize(_ALLOC_QUANTUM)
                )
                exit_alloc = (
                    exit_charges
                    if take == remaining
                    else (fill.charges * take / fill.quantity).quantize(_ALLOC_QUANTUM)
                )
                gross = (fill.price - lot.price) * take * book.direction
                net = gross - entry_alloc - exit_alloc
                closed.append(
                    ClosedPiece(
                        trade_id=f"{fill.fill_id}#{len(closed) + 1}",
                        instrument_key=fill.instrument_key,
                        product=product,
                        side=Side.BUY if book.direction > 0 else Side.SELL,
                        quantity=take,
                        entry_price=lot.price,
                        exit_price=fill.price,
                        entry_ts=lot.opened_at,
                        exit_ts=fill.ts,
                        gross_pnl=gross,
                        charges=entry_alloc + exit_alloc,
                        net_pnl=net,
                        decision_id=lot.decision_id,
                        exit_decision_id=fill.decision_id,
                        strategy=lot.strategy,
                        exit_reason=exit_reason,
                    )
                )
                book.realized += net
                lot.quantity -= take
                lot.charges -= entry_alloc
                remaining -= take
                exit_charges -= exit_alloc
                if lot.quantity == 0:
                    book.lots.popleft()
            if not book.lots:
                book.direction = 0
        if remaining:  # open, add, or the rest of a flip
            book.direction = sign
            book.lots.append(
                Lot(
                    quantity=remaining,
                    price=fill.price,
                    charges=exit_charges if closed else fill.charges,
                    opened_at=fill.ts,
                    decision_id=fill.decision_id,
                    strategy=strategy,
                    fill_id=fill.fill_id,
                )
            )
        book.updated_at = fill.ts
        return FillOutcome(self._position(key, book, fill.ts), tuple(closed))

    # -- reads -----------------------------------------------------------------------------------

    def has_seen(self, fill_id: str) -> bool:
        return fill_id in self._seen

    def quantity(self, instrument_key: str, product: Product) -> int:
        book = self._books.get((instrument_key, product))
        return 0 if book is None else book.quantity

    def lots(self, instrument_key: str, product: Product) -> tuple[Lot, ...]:
        book = self._books.get((instrument_key, product))
        return () if book is None else tuple(book.lots)

    def position(self, instrument_key: str, product: Product, now: datetime) -> Position:
        key = (instrument_key, product)
        return self._position(key, self._books.get(key, _Book()), now)

    def positions(self, now: datetime) -> list[Position]:
        return [self._position(k, b, b.updated_at or now) for k, b in sorted(self._books.items())]

    @property
    def realized_pnl(self) -> Decimal:
        return sum((b.realized for b in self._books.values()), Decimal(0))

    def market_value(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Sum of quantity x mark over open positions (raises if a mark is missing)."""
        total = Decimal(0)
        for (instrument_key, _), book in self._books.items():
            qty = book.quantity
            if qty:
                if instrument_key not in marks:
                    raise KeyError(f"no mark for open position {instrument_key}")
                total += qty * marks[instrument_key]
        return total

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        return self.cash + self.market_value(marks)

    def _position(self, key: tuple[str, Product], book: _Book, now: datetime) -> Position:
        qty = book.quantity
        avg = None
        if qty:
            shares = sum(lot.quantity for lot in book.lots)
            avg = sum((lot.price * lot.quantity for lot in book.lots), Decimal(0)) / shares
        return Position(
            book_id=self.book_id,
            instrument_key=key[0],
            product=key[1],
            quantity=qty,
            avg_price=avg,
            realized_pnl=book.realized,
            updated_at=book.updated_at or now,
        )
