"""
Local Paper Trading Engine

Simulates a broker exchange locally for 100% free paper trading.
Maintains virtual wallet, positions, and order history.

Features:
- Virtual wallet with configurable starting balance
- Position tracking with P&L calculation
- Market and limit order simulation
- Persistent state via JSON file
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.config import get_settings
from src.execution.costs import CostModel

logger = logging.getLogger(__name__)

# Default state file location
STATE_FILE = Path("paper_wallet.json")

# Cap persisted order history (the journal is the durable, unbounded record).
MAX_PERSISTED_ORDERS = 5000


@dataclass
class Position:
    """Represents an open position."""

    position_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    entry_price: float
    entry_time: str
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    entry_charges: float = 0.0  # charges paid on entry (for net-PnL attribution)
    # MAE/MFE tracking for trade quality analysis
    mae: float = 0.0  # Maximum Adverse Excursion (worst drawdown)
    mfe: float = 0.0  # Maximum Favorable Excursion (best unrealized profit)
    highest_price: float = 0.0
    lowest_price: float = 0.0
    stop_loss: float = 0.0
    target_price: float = 0.0
    strategy: str = ""

    def __post_init__(self):
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price
        if self.lowest_price == 0.0:
            self.lowest_price = self.entry_price

    def update_pnl(self, current_price: float):
        """Update unrealized P&L and MAE/MFE based on current price."""
        self.current_price = current_price
        self.highest_price = max(self.highest_price, current_price)
        self.lowest_price = min(self.lowest_price, current_price)

        if self.side == "BUY":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
            self.mfe = max(self.mfe, (self.highest_price - self.entry_price) * self.quantity)
            self.mae = max(self.mae, (self.entry_price - self.lowest_price) * self.quantity)
        else:  # SELL (short)
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity
            self.mfe = max(self.mfe, (self.entry_price - self.lowest_price) * self.quantity)
            self.mae = max(self.mae, (self.highest_price - self.entry_price) * self.quantity)

        if self.entry_price > 0:
            self.unrealized_pnl_pct = (
                self.unrealized_pnl / (self.entry_price * self.quantity)
            ) * 100

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    """Represents an executed order."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str  # "MARKET" or "LIMIT"
    price: float
    status: str  # "FILLED", "PENDING", "CANCELLED"
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperWalletState:
    """Persistent state for the paper wallet."""

    balance: float
    initial_balance: float
    positions: list[dict]
    orders: list[dict]
    realized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PaperWalletState":
        return cls(**data)


class LocalPaperEngine:
    """
    Local paper trading engine that simulates a broker.

    Provides:
    - Virtual wallet management
    - Position tracking
    - Order execution (market orders filled at current price)
    - P&L calculation
    - Persistent state
    """

    def __init__(
        self,
        initial_balance: float | None = None,
        state_file: Path | None = None,
        cost_model: CostModel | None = None,
    ):
        """
        Initialize the paper trading engine.

        Args:
            initial_balance: Starting balance in INR (default from config)
            state_file: Path to state file for persistence
            cost_model: Slippage/fee model (defaults to CostModel.from_settings();
                pass CostModel.zero() for ideal fills)
        """
        settings = get_settings()
        self.initial_balance = initial_balance or settings.paper_wallet_balance
        self.state_file = state_file or STATE_FILE
        self.cost_model = cost_model or CostModel.from_settings(settings)

        self.balance = self.initial_balance
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        # Load existing state if available
        self._load_state()

    def _load_state(self):
        """Load state from file if exists."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)

                state = PaperWalletState.from_dict(data)
                self.balance = state.balance
                self.initial_balance = state.initial_balance
                self.realized_pnl = state.realized_pnl
                self.total_trades = state.total_trades
                self.winning_trades = state.winning_trades
                self.losing_trades = state.losing_trades

                # Restore positions
                for pos_data in state.positions:
                    pos = Position(**pos_data)
                    self.positions[pos.position_id] = pos

                # Restore orders
                for order_data in state.orders:
                    self.orders.append(Order(**order_data))

                logger.info(
                    f"Loaded paper wallet state: ₹{self.balance:,.2f} balance, {len(self.positions)} positions"
                )

            except Exception as e:
                # Do NOT silently discard the wallet — quarantine the corrupt file (so it
                # can be inspected/recovered) and surface the problem loudly.
                backup = self.state_file.with_suffix(
                    f".corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
                )
                try:
                    self.state_file.rename(backup)
                    logger.error(
                        "Failed to load paper wallet state (%s). Quarantined the corrupt file "
                        "to %s and started fresh.",
                        e,
                        backup,
                    )
                except OSError:
                    logger.error("Failed to load paper wallet state (%s); starting fresh.", e)

    def _save_state(self):
        """Save current state to file atomically (temp file + os.replace)."""
        try:
            state = PaperWalletState(
                balance=self.balance,
                initial_balance=self.initial_balance,
                positions=[p.to_dict() for p in self.positions.values()],
                orders=[o.to_dict() for o in self.orders[-MAX_PERSISTED_ORDERS:]],
                realized_pnl=self.realized_pnl,
                total_trades=self.total_trades,
                winning_trades=self.winning_trades,
                losing_trades=self.losing_trades,
                created_at=self.orders[0].timestamp if self.orders else datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )

            tmp_file = self.state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.state_file)  # atomic on POSIX and Windows

            logger.debug("Paper wallet state saved")

        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        current_price: float,
        order_type: str = "MARKET",
    ) -> Order:
        """
        Place an order (immediately filled for market orders).

        Args:
            symbol: Stock symbol
            side: "BUY" or "SELL"
            quantity: Number of shares
            current_price: Current market price
            order_type: "MARKET" or "LIMIT"

        Returns:
            Order object with fill details
        """
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"

        # Limit orders are not simulated yet — return PENDING unchanged.
        if order_type != "MARKET":
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=current_price,
                status="PENDING",
                timestamp=datetime.now().isoformat(),
            )

        side = side.upper()
        # Apply adverse slippage to get the actual fill price.
        fill_price = self.cost_model.fill_price(current_price, side)

        def _rejected() -> Order:
            return Order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=fill_price,
                status="REJECTED",
                timestamp=datetime.now().isoformat(),
            )

        # An order in the opposite direction closes/covers an existing position first.
        opposite = "SELL" if side == "BUY" else "BUY"
        matching = next(
            (p for p in self.positions.values() if p.symbol == symbol and p.side == opposite),
            None,
        )

        remaining = quantity
        if matching is not None:
            close_qty = min(quantity, matching.quantity)
            close_notional = fill_price * close_qty
            close_charges = self.cost_model.charges(close_notional, side)
            if matching.side == "BUY":  # selling to close a long
                gross = (fill_price - matching.entry_price) * close_qty
            else:  # buying to cover a short
                gross = (matching.entry_price - fill_price) * close_qty
            entry_charges_prorata = matching.entry_charges * (close_qty / matching.quantity)
            net_pnl = gross - entry_charges_prorata - close_charges

            # Release the principal committed on entry, add the gross gain, pay exit charges.
            self.balance += matching.entry_price * close_qty + gross - close_charges
            self.realized_pnl += net_pnl
            self.total_trades += 1
            if net_pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1

            if close_qty >= matching.quantity:
                del self.positions[matching.position_id]
            else:
                matching.quantity -= close_qty
                matching.entry_charges -= entry_charges_prorata

            logger.info(
                f"{side} filled (close): {close_qty} {symbol} @ ₹{fill_price:,.2f} | "
                f"net P&L: ₹{net_pnl:+,.2f}"
            )
            remaining = quantity - close_qty

        if remaining > 0:
            # Opening a new position (long for BUY, short for SELL).
            open_notional = fill_price * remaining
            open_charges = self.cost_model.charges(open_notional, side)
            required = open_notional + open_charges
            if required > self.balance:
                logger.warning(
                    f"Insufficient balance for {symbol}: need ₹{required:,.2f}, "
                    f"have ₹{self.balance:,.2f}"
                )
                # If we already closed part of an opposite position above, that stands;
                # only the new-open remainder is rejected.
                if matching is None:
                    return _rejected()
            else:
                self.balance -= required
                self._open_position(symbol, side, remaining, fill_price, open_charges)
                logger.info(
                    f"{side} filled (open): {remaining} {symbol} @ ₹{fill_price:,.2f} | "
                    f"Balance: ₹{self.balance:,.2f}"
                )

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=fill_price,
            status="FILLED",
            timestamp=datetime.now().isoformat(),
        )
        self.orders.append(order)
        self._save_state()
        return order

    def _open_position(
        self, symbol: str, side: str, quantity: int, fill_price: float, charges: float
    ) -> Position:
        """Create and register a new open position (long or short)."""
        position = Position(
            position_id=f"POS-{uuid4().hex[:8]}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            entry_time=datetime.now().isoformat(),
            current_price=fill_price,
            entry_charges=charges,
        )
        self.positions[position.position_id] = position
        return position

    def update_positions_pnl(self, market_prices: dict[str, float]):
        """
        Update unrealized P&L for all positions based on current prices.

        Args:
            market_prices: Dictionary of symbol -> current price
        """
        for position in self.positions.values():
            if position.symbol in market_prices:
                position.update_pnl(market_prices[position.symbol])

    def get_balance(self) -> float:
        """Get current cash balance."""
        return self.balance

    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        return list(self.positions.values())

    def get_total_value(self) -> float:
        """
        Total portfolio value = cash + committed capital + unrealized P&L.

        Uses committed capital (entry notional + entry charges) plus unrealized P&L rather
        than naive market value, so short positions are valued correctly and opening a
        position doesn't change net worth.
        """
        positions_value = sum(
            p.entry_price * p.quantity + p.entry_charges + p.unrealized_pnl
            for p in self.positions.values()
        )
        return self.balance + positions_value

    def get_unrealized_pnl(self) -> float:
        """Get total unrealized P&L across all positions."""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def get_win_rate(self) -> float:
        """Get win rate percentage."""
        if self.total_trades == 0:
            return 0.0
        return (self.winning_trades / self.total_trades) * 100

    def get_stats(self) -> dict[str, Any]:
        """Get comprehensive trading statistics."""
        return {
            "balance": self.balance,
            "initial_balance": self.initial_balance,
            "total_value": self.get_total_value(),
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.get_unrealized_pnl(),
            "total_pnl": self.realized_pnl + self.get_unrealized_pnl(),
            "return_pct": ((self.get_total_value() - self.initial_balance) / self.initial_balance)
            * 100,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.get_win_rate(),
            "open_positions": len(self.positions),
        }

    def reset(self):
        """Reset the paper wallet to initial state."""
        self.balance = self.initial_balance
        self.positions = {}
        self.orders = []
        self.realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        if self.state_file.exists():
            self.state_file.unlink()

        logger.info(f"Paper wallet reset to ₹{self.initial_balance:,.2f}")


def test_paper_engine():
    """Test the paper trading engine."""
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("[PAPER] RakshaQuant - Local Paper Engine Test")
    print("=" * 60)

    # Create engine with 10 lakh balance
    engine = LocalPaperEngine(initial_balance=1000000)

    print(f"\n[INIT] Starting balance: ₹{engine.get_balance():,.2f}")

    # Simulate buying RELIANCE
    print("\n[ORDER] Placing BUY order for RELIANCE...")
    order1 = engine.place_order(
        symbol="RELIANCE",
        side="BUY",
        quantity=10,
        current_price=2500.0,
    )
    print(f"  Order: {order1.status} @ ₹{order1.price:,.2f}")
    print(f"  Balance: ₹{engine.get_balance():,.2f}")

    # Simulate price going up
    print("\n[UPDATE] Price increased to ₹2550...")
    engine.update_positions_pnl({"RELIANCE": 2550.0})

    positions = engine.get_positions()
    for pos in positions:
        print(f"  Position: {pos.quantity} {pos.symbol} @ ₹{pos.entry_price:,.2f}")
        print(f"  Unrealized P&L: ₹{pos.unrealized_pnl:+,.2f} ({pos.unrealized_pnl_pct:+.2f}%)")

    # Sell to realize profit
    print("\n[ORDER] Placing SELL order to close position...")
    order2 = engine.place_order(
        symbol="RELIANCE",
        side="SELL",
        quantity=10,
        current_price=2550.0,
    )
    print(f"  Order: {order2.status} @ ₹{order2.price:,.2f}")

    # Show stats
    print("\n[STATS] Trading Statistics:")
    stats = engine.get_stats()
    for key, value in stats.items():
        if isinstance(value, float):
            print(
                f"  {key}: ₹{value:,.2f}"
                if "pnl" in key or "balance" in key or "value" in key
                else f"  {key}: {value:.2f}%"
            )
        else:
            print(f"  {key}: {value}")

    print("\n" + "=" * 60)
    print("[SUCCESS] Paper trading engine working!")
    print("=" * 60)


if __name__ == "__main__":
    test_paper_engine()
