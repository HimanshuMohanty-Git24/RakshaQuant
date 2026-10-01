"""Plan M5 acceptance: a full paper session on a recorded tape via ReplayClock, end to end -
signals → risk → fills → exits → report - with a lineage for every trade."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from src.config.limits import load_risk_limits
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.types import Bar, Instrument, MarketDataSource, Product, Quote, Timeframe
from src.engine.runner import EngineConfig, build_engine
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.store.event_store import EventStore
from src.store.tape import TapeWriter, read_bars, read_quotes
from src.utils.market_time import IST

DAY = date(2026, 10, 5)  # a Monday; Friday 2 Oct is a holiday, so the last session is 1 Oct
INFY = Instrument.nse_equity("INFY", sector="IT")
TCS = Instrument.nse_equity("TCS", sector="IT")


def at(hh: int, mm: int, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm), IST)


def history(key: str, seed: int, last: float, n: int = 250) -> list[Bar]:
    """Settled daily bars ending 1 Oct; seed 3 makes the real momentum strategy fire on the
    last bar (RSI 46.9 with a positive MACD histogram, ATR ~1.8%)."""
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    close = close / close[-1] * last
    days = pd.bdate_range(end="2026-10-01", periods=n)
    bars = []
    for adjusted in (False, True):
        bars += [
            Bar(instrument_key=key, timeframe=Timeframe.D1, session_date=d.date(), open=c,
                high=c * 1.008, low=c * 0.992, close=c, volume=3_000_000, is_settled=True,
                adjusted=adjusted, source=MarketDataSource.YFINANCE)
            for d, c in zip(days, close, strict=True)
        ]  # fmt: skip
    return bars


def session_quotes(key: str, path: list[tuple[datetime, float]], prev_close: float) -> list[Quote]:
    out, minute, volume = [], at(9, 15), 0
    points = iter(path)
    nxt = next(points)
    price = nxt[1]
    while minute <= at(15, 30):
        while nxt is not None and minute >= nxt[0]:
            price = nxt[1]
            nxt = next(points, None)
        volume += 50_000
        out.append(Quote(instrument_key=key, ltp=round(price, 2), prev_close=prev_close,
                         volume_cum=volume, exchange_ts=minute - timedelta(minutes=15),
                         receipt_ts=minute, source=MarketDataSource.YFINANCE,
                         is_delayed=True))  # fmt: skip
        minute += timedelta(minutes=1)
    return out


def ramp(start: datetime, end: datetime, p0: float, p1: float) -> list[tuple[datetime, float]]:
    steps = int((end - start).total_seconds() // 60)
    return [(start + timedelta(minutes=i), p0 + (p1 - p0) * i / steps) for i in range(steps + 1)]


def record_tape(root: Path) -> None:
    tape = TapeWriter(root)
    tape.add_bars(history(INFY.key, 3, 1000.0) + history(TCS.key, 8, 4000.0)
                  + history("NSE:INDEX:NIFTY50", 5, 25_000.0), recorded_on=DAY)  # fmt: skip
    infy = [(at(9, 15), 1001.0), *ramp(at(9, 30), at(13, 0), 1002.0, 1070.0), (at(13, 1), 1070.0)]
    tape.add_quotes(session_quotes(INFY.key, infy, 1000.0))
    tape.add_quotes(session_quotes(TCS.key, [(at(9, 15), 4001.0)], 4000.0))
    tape.flush()


async def run_day(root: Path, store: EventStore, start: datetime) -> tuple[int, object]:
    clock = ReplayClock(start)
    engine = build_engine(
        config=EngineConfig(environment="paper"),
        clock=clock, calendar=get_calendar(), store=store,
        quotes=TapeQuoteSource(read_quotes(root, DAY), clock=clock),
        history=TapeHistorySource(read_bars(root, DAY)),
        universe=[INFY, TCS], limits=load_risk_limits(),
    )  # fmt: skip
    task = asyncio.create_task(engine.run())
    while not task.done():
        await clock.advance(30)
    return task.result(), engine


async def test_a_full_session_replays_from_tape_with_lineage(tmp_path):
    record_tape(tmp_path / "tape")
    with EventStore(tmp_path / "rq.db") as store:
        code, engine = await run_day(tmp_path / "tape", store, at(8, 50))
        assert code == 0

        states = [e.payload.current.value for e in store.read(types=["SessionStateChanged"])]
        assert states == ["PRE_OPEN", "OPEN", "ENTRY_WINDOW", "MONITOR", "CLOSE", "REPORT",
                          "EXIT"]  # fmt: skip
        (regime,) = store.read(types=["RegimeComputed"])
        assert regime.payload.session_date == DAY

        # signals -> risk -> fills -> exits
        signals = [e.payload.signal for e in store.read(types=["SignalGenerated"])]
        momentum = [s for s in signals if s.strategy == "momentum" and s.instrument_key == INFY.key]
        assert len(momentum) == 1 and not momentum[0].is_shadow
        (trade,) = store.query("SELECT * FROM trades")
        assert trade["instrument_key"] == INFY.key and trade["exit_reason"] == "target"
        assert Decimal(trade["net_pnl"]) > 0

        # every trade has a lineage: signal -> intent -> risk decision -> order -> fill
        entry = {e.type for e in store.read(decision_id=trade["decision_id"])}
        assert {"SignalGenerated", "OrderIntentProposed", "RiskDecision", "OrderSubmitted",
                "FillReceived"} <= entry  # fmt: skip
        exit_ = {e.type for e in store.read(decision_id=trade["exit_decision_id"])}
        assert {"RiskDecision", "OrderSubmitted", "FillReceived"} <= exit_

        # entries use fresh quotes: the arrival price is the 09:20 quote, the fill comes after
        entry_decision = next(e.payload for e in store.read(decision_id=trade["decision_id"],
                                                             types=["RiskDecision"]))  # fmt: skip
        assert entry_decision.ref_price == Decimal("1001.0")
        assert entry_decision.snapshot.quote_age_s is not None
        assert entry_decision.snapshot.quote_age_s <= 1200
        assert datetime.fromisoformat(trade["entry_ts"]) > at(9, 20)

        assert store.read(types=["MarkToMarket"])  # the day's report
        assert engine.oms.book.quantity(INFY.key, Product.CNC) == 0  # type: ignore[attr-defined]
        alerts = [e.payload for e in store.read(types=["Alert"]) if e.payload.level == "CRITICAL"]
        assert alerts == []


async def test_a_restart_mid_session_carries_the_position(tmp_path):
    """Stop after the entry fills; a new process restores the book and keeps the position priced."""
    record_tape(tmp_path / "tape")
    with EventStore(tmp_path / "rq.db") as store:
        clock = ReplayClock(at(8, 50))
        engine = build_engine(
            config=EngineConfig(environment="paper"), clock=clock, calendar=get_calendar(),
            store=store, quotes=TapeQuoteSource(read_quotes(tmp_path / "tape", DAY), clock=clock),
            history=TapeHistorySource(read_bars(tmp_path / "tape", DAY)),
            universe=[INFY, TCS], limits=load_risk_limits(),
        )  # fmt: skip
        task = asyncio.create_task(engine.run())
        while clock.now() < at(9, 40):
            await clock.advance(30)
        task.cancel()  # the process dies
        await asyncio.gather(task, return_exceptions=True)
        await engine.tasks.stop()
        held = engine.oms.book.quantity(INFY.key, Product.CNC)
        assert held > 0

        # The new process's universe no longer lists INFY: it must still be priced.
        again = build_engine(
            config=EngineConfig(environment="paper"), clock=clock, calendar=get_calendar(),
            store=store, quotes=TapeQuoteSource(read_quotes(tmp_path / "tape", DAY), clock=clock),
            history=TapeHistorySource(read_bars(tmp_path / "tape", DAY)),
            universe=[TCS], limits=load_risk_limits(),
        )  # fmt: skip
        assert again.oms.book.quantity(INFY.key, Product.CNC) == held
        assert INFY.key in again.market.instruments
        await again.market.poll()
        assert INFY.key in again.market.marks()
        assert (await again.oms.reconcile()).in_sync
