"""
Dynamic Exit Manager

Manages position exits using trailing stops, time exits, partial profit taking,
regime change exits, and breakeven stop movement. Tracks MAE/MFE per position.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExitRule:
    """Result of an exit check."""

    should_exit: bool
    reason: str
    exit_type: str
    partial_pct: float = 1.0
    priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_exit": self.should_exit,
            "reason": self.reason,
            "exit_type": self.exit_type,
            "partial_pct": self.partial_pct,
            "priority": self.priority,
        }


@dataclass
class ManagedPosition:
    """A position tracked by the exit manager."""

    position_id: str
    symbol: str
    side: str
    quantity: int
    entry_price: float
    entry_time: datetime
    stop_loss: float
    target_price: float
    strategy: str = ""
    regime_at_entry: str = ""
    current_stop: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    breakeven_moved: bool = False
    partial_taken: bool = False
    mae: float = 0.0
    mfe: float = 0.0

    def __post_init__(self):
        if self.current_stop == 0.0:
            self.current_stop = self.stop_loss
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price
        if self.lowest_price == 0.0:
            self.lowest_price = self.entry_price

    def update_price(self, current_price: float):
        self.highest_price = max(self.highest_price, current_price)
        self.lowest_price = min(self.lowest_price, current_price)
        if self.side == "BUY":
            self.mfe = max(self.mfe, (self.highest_price - self.entry_price) * self.quantity)
            self.mae = max(self.mae, (self.entry_price - self.lowest_price) * self.quantity)
        else:
            self.mfe = max(self.mfe, (self.entry_price - self.lowest_price) * self.quantity)
            self.mae = max(self.mae, (self.highest_price - self.entry_price) * self.quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "current_stop": self.current_stop,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "breakeven_moved": self.breakeven_moved,
            "partial_taken": self.partial_taken,
            "mae": self.mae,
            "mfe": self.mfe,
            "strategy": self.strategy,
            "regime_at_entry": self.regime_at_entry,
        }


class ExitManager:
    """Manages position exits with trailing stops, time limits, targets, and regime changes."""

    def __init__(
        self,
        trailing_atr_multiplier: float = 1.5,
        breakeven_r_threshold: float = 1.0,
        max_hold_minutes: int = 240,
        partial_profit_r: float = 1.0,
        partial_exit_pct: float = 0.5,
        stale_trade_minutes: int = 60,
        stale_trade_min_pnl_pct: float = 0.5,
        state_file: Path | None = None,
    ):
        self.trailing_atr_multiplier = trailing_atr_multiplier
        self.breakeven_r_threshold = breakeven_r_threshold
        self.max_hold_minutes = max_hold_minutes
        self.partial_profit_r = partial_profit_r
        self.partial_exit_pct = partial_exit_pct
        self.stale_trade_minutes = stale_trade_minutes
        self.stale_trade_min_pnl_pct = stale_trade_min_pnl_pct
        self.state_file = state_file
        self._positions: dict[str, ManagedPosition] = {}
        self._load()

    def _load(self) -> None:
        """Restore tracked positions (trailing stops, breakeven, MAE/MFE) after a restart."""
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            for pid, pos_data in data.items():
                pos_data = dict(pos_data)
                pos_data["entry_time"] = datetime.fromisoformat(pos_data["entry_time"])
                self._positions[pid] = ManagedPosition(**pos_data)
            logger.info(
                "Restored %d managed positions from %s", len(self._positions), self.state_file
            )
        except Exception as e:
            logger.warning("Could not load exit-manager state (%s); starting empty.", e)

    def _save(self) -> None:
        """Persist tracked positions atomically (temp file + os.replace)."""
        if self.state_file is None:
            return
        try:
            data = {pid: pos.to_dict() for pid, pos in self._positions.items()}
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Could not persist exit-manager state (%s).", e)

    def register_position(
        self,
        position_id: str,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        target_price: float,
        strategy: str = "",
        regime: str = "",
        entry_time: datetime | None = None,
    ) -> ManagedPosition:
        pos = ManagedPosition(
            position_id=position_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=entry_time or datetime.now(),
            stop_loss=stop_loss,
            target_price=target_price,
            strategy=strategy,
            regime_at_entry=regime,
        )
        self._positions[position_id] = pos
        self._save()
        logger.info(f"Exit manager tracking: {symbol} {side} @ {entry_price:,.2f}")
        return pos

    def unregister_position(self, position_id: str) -> ManagedPosition | None:
        pos = self._positions.pop(position_id, None)
        self._save()
        return pos

    def clear(self) -> None:
        """Drop all tracked positions (e.g. after a kill-switch flatten)."""
        self._positions.clear()
        self._save()

    def check_exits(
        self,
        market_prices: dict[str, float],
        current_regime: str = "",
        atr_values: dict[str, float] | None = None,
    ) -> list[tuple[ManagedPosition, ExitRule]]:
        atr_values = atr_values or {}
        exits = []
        for pos in list(self._positions.values()):
            price = market_prices.get(pos.symbol)
            if price is None:
                continue
            pos.update_price(price)
            rules = []
            # 1. Stop loss
            r = self._check_stop(pos, price)
            if r.should_exit:
                rules.append(r)
            # 2. Target
            r = self._check_target(pos, price)
            if r.should_exit:
                rules.append(r)
            # 3. Trailing stop
            atr = atr_values.get(pos.symbol, 0)
            r = self._check_trailing(pos, price, atr)
            if r.should_exit:
                rules.append(r)
            # 4. Time exit
            r = self._check_time(pos)
            if r.should_exit:
                rules.append(r)
            # 5. Stale trade
            r = self._check_stale(pos, price)
            if r.should_exit:
                rules.append(r)
            # 6. Regime change
            if current_regime:
                r = self._check_regime(pos, current_regime)
                if r.should_exit:
                    rules.append(r)
            # 7. Partial profit
            r = self._check_partial(pos, price)
            if r.should_exit:
                rules.append(r)
            # Manage trailing stop movement
            if atr > 0:
                self._update_trailing(pos, price, atr)
            self._check_breakeven(pos, price)
            if rules:
                rules.sort(key=lambda x: x.priority, reverse=True)
                exits.append((pos, rules[0]))
        # Persist trailing-stop / breakeven / MAE-MFE updates made this cycle.
        self._save()
        return exits

    def _check_stop(self, pos, price):
        hit = (price <= pos.current_stop) if pos.side == "BUY" else (price >= pos.current_stop)
        return ExitRule(
            should_exit=hit,
            reason=f"Stop hit at {pos.current_stop:,.2f}",
            exit_type="stop_loss",
            priority=100,
        )

    def _check_target(self, pos, price):
        hit = (price >= pos.target_price) if pos.side == "BUY" else (price <= pos.target_price)
        return ExitRule(
            should_exit=hit,
            reason=f"Target hit at {pos.target_price:,.2f}",
            exit_type="target_hit",
            priority=90,
        )

    def _check_trailing(self, pos, price, atr):
        if atr <= 0:
            return ExitRule(should_exit=False, reason="", exit_type="trailing_stop")
        dist = atr * self.trailing_atr_multiplier
        if pos.side == "BUY":
            trail = pos.highest_price - dist
            if trail > pos.current_stop and trail > pos.entry_price and price <= trail:
                return ExitRule(
                    should_exit=True,
                    reason=f"Trailing stop at {trail:,.2f} (peak: {pos.highest_price:,.2f})",
                    exit_type="trailing_stop",
                    priority=85,
                )
        else:
            trail = pos.lowest_price + dist
            if trail < pos.current_stop and trail < pos.entry_price and price >= trail:
                return ExitRule(
                    should_exit=True,
                    reason=f"Trailing stop at {trail:,.2f}",
                    exit_type="trailing_stop",
                    priority=85,
                )
        return ExitRule(should_exit=False, reason="", exit_type="trailing_stop")

    def _check_time(self, pos):
        mins = (datetime.now() - pos.entry_time).total_seconds() / 60
        if mins >= self.max_hold_minutes:
            return ExitRule(
                should_exit=True,
                reason=f"Max hold {mins:.0f} min",
                exit_type="time_exit",
                priority=70,
            )
        return ExitRule(should_exit=False, reason="", exit_type="time_exit")

    def _check_stale(self, pos, price):
        mins = (datetime.now() - pos.entry_time).total_seconds() / 60
        if mins < self.stale_trade_minutes:
            return ExitRule(should_exit=False, reason="", exit_type="stale_exit")
        pnl_pct = (
            ((price - pos.entry_price) / pos.entry_price * 100)
            if pos.side == "BUY"
            else ((pos.entry_price - price) / pos.entry_price * 100)
        )
        if abs(pnl_pct) < self.stale_trade_min_pnl_pct:
            return ExitRule(
                should_exit=True,
                reason=f"Stale: {pnl_pct:+.2f}% after {mins:.0f}min",
                exit_type="stale_exit",
                priority=50,
            )
        return ExitRule(should_exit=False, reason="", exit_type="stale_exit")

    def _check_regime(self, pos, regime):
        adverse = {
            ("trending_up", "trending_down"),
            ("trending_up", "volatile"),
            ("trending_down", "trending_up"),
            ("trending_down", "volatile"),
        }
        if (pos.regime_at_entry, regime) in adverse:
            return ExitRule(
                should_exit=True,
                reason=f"Regime: {pos.regime_at_entry}->{regime}",
                exit_type="regime_change",
                priority=60,
            )
        return ExitRule(should_exit=False, reason="", exit_type="regime_change")

    def _check_partial(self, pos, price):
        if pos.partial_taken:
            return ExitRule(should_exit=False, reason="", exit_type="partial")
        risk = abs(pos.entry_price - pos.stop_loss)
        if risk <= 0:
            return ExitRule(should_exit=False, reason="", exit_type="partial")
        r = (
            ((price - pos.entry_price) / risk)
            if pos.side == "BUY"
            else ((pos.entry_price - price) / risk)
        )
        if r >= self.partial_profit_r:
            return ExitRule(
                should_exit=True,
                reason=f"Partial at {r:.1f}R",
                exit_type="partial",
                partial_pct=self.partial_exit_pct,
                priority=40,
            )
        return ExitRule(should_exit=False, reason="", exit_type="partial")

    def _update_trailing(self, pos, price, atr):
        dist = atr * self.trailing_atr_multiplier
        if pos.side == "BUY":
            new = pos.highest_price - dist
            if new > pos.current_stop and new > pos.entry_price:
                pos.current_stop = new
        else:
            new = pos.lowest_price + dist
            if new < pos.current_stop and new < pos.entry_price:
                pos.current_stop = new

    def _check_breakeven(self, pos, price):
        if pos.breakeven_moved:
            return
        risk = abs(pos.entry_price - pos.stop_loss)
        if risk <= 0:
            return
        r = (
            ((price - pos.entry_price) / risk)
            if pos.side == "BUY"
            else ((pos.entry_price - price) / risk)
        )
        if r >= self.breakeven_r_threshold:
            if pos.side == "BUY":
                pos.current_stop = max(pos.current_stop, pos.entry_price)
            else:
                pos.current_stop = min(pos.current_stop, pos.entry_price)
            pos.breakeven_moved = True
            logger.info(f"Breakeven stop for {pos.symbol} at {r:.1f}R")

    def get_managed_positions(self) -> list[ManagedPosition]:
        return list(self._positions.values())

    def get_position(self, position_id: str) -> ManagedPosition | None:
        return self._positions.get(position_id)
