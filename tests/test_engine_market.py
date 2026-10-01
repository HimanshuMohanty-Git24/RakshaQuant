"""Plan M5.6 (part 1): OMS restore after a restart, tape replay sources, the market service."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.brokers.simulated.costs import NSECostSchedule
from src.domain.clock import ReplayClock
from src.domain.events import Alert
from src.domain.sink import RecordingSink
from src.domain.types import MarketDataSource, OrderStatus, OrderType, Product, Quote, Side
from src.engine.market import INDEX_KEY, MarketService
from src.marketdata.history import parse_daily_frame, record_history, series_from_bars
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.oms.oms import OMS
from src.oms.position_book import PositionBook
from src.store.sink import StoreSink
from src.store.tape import TapeWriter, read_bars
from tests.oms_harness import INFY, harness, intent, unchecked
from tests.test_marketdata_history import TICKERS, TODAY, daily_frame

T0 = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


# --- OMS restore ------------------------------------------------------------------------------------


async def test_a_restarted_oms_rebuilds_orders_and_the_book_from_events(tmp_path):
    with harness(tmp_path, costs=NSECostSchedule.from_yaml()) as h:
        await h.oms.start()
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 100))
        h.quote(1001.0)  # fills
        await h.oms.submit(intent(Side.SELL, 40, leg="partial"))
        h.quote(1010.0)  # partial exit fills
        stop = await h.oms.submit(intent(Side.SELL, 60, leg="stop", order_type=OrderType.SL_M,
                                         trigger="950"))  # fmt: skip
        assert stop.order is not None
        await h.oms.cancel(stop.order.client_order_id)
        await h.oms.submit(intent(Side.SELL, 10, leg="stop2", order_type=OrderType.SL_M,
                                  trigger="951"))  # resting  # fmt: skip

        reborn = OMS(book_id="A", broker=h.broker, book=PositionBook("A", Decimal("1000000")),
                     sink=StoreSink(h.store, h.clock, "oms"), clock=h.clock, gate=unchecked)  # fmt: skip
        applied = reborn.restore(h.store.read(book_id="A"))
        assert applied > 0
        assert {k: (o.status, o.filled_qty, o.avg_fill_price, o.broker_order_id)
                for k, o in reborn.orders.items()} == {
            k: (o.status, o.filled_qty, o.avg_fill_price, o.broker_order_id)
            for k, o in h.oms.orders.items()
        }  # fmt: skip
        statuses = sorted(o.status for o in reborn.orders.values())
        assert statuses.count(OrderStatus.FILLED) == 2 and OrderStatus.CANCELLED in statuses
        assert reborn.book.cash == h.book.cash
        assert reborn.book.realized_pnl == h.book.realized_pnl
        assert reborn.book.quantity(INFY.key, Product.CNC) == 60
        assert (await reborn.reconcile()).in_sync  # the broker agrees with the rebuilt OMS
        with pytest.raises(RuntimeError, match="fresh"):
            reborn.restore(h.store.read(book_id="A"))


async def test_a_fill_for_an_unknown_order_is_recorded_and_restored(tmp_path):
    with harness(tmp_path) as h:
        h.quote(1000.0)
        await h.oms.submit(intent(Side.BUY, 50))
        h.oms.orders.clear()  # the OMS "forgot" the order
        await h.oms.start()
        await h.oms.reconcile()  # adopts the broker's fill as an unknown-order fill
        h.quote(1001.0)
        assert h.book.quantity(INFY.key, Product.CNC) == 50
        reborn = OMS(book_id="A", broker=h.broker, book=PositionBook("A", Decimal("1000000")),
                     sink=StoreSink(h.store, h.clock, "oms"), clock=h.clock, gate=unchecked)  # fmt: skip
        reborn.restore(h.store.read(book_id="A"))
        assert reborn.book.quantity(INFY.key, Product.CNC) == 50


# --- tape replay ----------------------------------------------------------------------------------------


def q(key: str, ltp: float, minutes: int) -> Quote:
    ts = T0 + timedelta(minutes=minutes)
    return Quote(instrument_key=key, ltp=ltp, prev_close=990.0, volume_cum=minutes * 1000 + 1,
                 exchange_ts=ts - timedelta(minutes=15), receipt_ts=ts,
                 source=MarketDataSource.YFINANCE)  # fmt: skip


async def test_the_tape_quote_source_serves_what_was_known_at_the_time():
    clock = ReplayClock(T0)
    source = TapeQuoteSource([q("A", 10, 2), q("A", 11, 1), q("B", 20, 5)], clock=clock)
    assert (await source.poll()).quotes == {}
    await clock.advance(90)
    assert {k: v.ltp for k, v in (await source.poll()).quotes.items()} == {"A": 11}
    await clock.advance(300)
    assert {k: v.ltp for k, v in (await source.poll()).quotes.items()} == {"A": 10, "B": 20}
    assert (await source.poll()).quotes == {}  # nothing new since
    assert source.last_quotes["A"].ltp == 10


def test_taped_bars_rebuild_the_same_history(tmp_path):
    frame = daily_frame(["INFY.NS", "TCS.NS"], dividends={70: 8.5})
    original = parse_daily_frame(frame, TICKERS, settled_before=TODAY)
    record_history(original, tape=TapeWriter(tmp_path), recorded_on=TODAY)
    rebuilt = series_from_bars(read_bars(tmp_path, TODAY), settled_before=TODAY)
    for key, series in original.series.items():
        again = rebuilt.series[key]
        assert again.raw().equals(series.raw().astype(again.raw().dtypes))
        assert again.adjusted()["close"].to_numpy() == pytest.approx(
            series.adjusted()["close"].to_numpy(), rel=1e-12
        )


async def test_the_tape_history_source_reports_what_is_missing(tmp_path):
    frame = daily_frame(["INFY.NS"])
    record_history(parse_daily_frame(frame, {"NSE:EQ:INFY": "INFY.NS"}, settled_before=TODAY),
                   tape=TapeWriter(tmp_path), recorded_on=TODAY)  # fmt: skip
    source = TapeHistorySource(read_bars(tmp_path, TODAY))
    result = await source.fetch([INFY], settled_before=TODAY, expected_last=date(2026, 10, 1),
                                extra={INDEX_KEY: "^NSEI"})  # fmt: skip
    assert set(result.series) == {INFY.key} and result.failed == {INDEX_KEY: "not on the tape"}


# --- the market service ---------------------------------------------------------------------------------


async def test_the_market_service_feeds_listeners_in_order_and_isolates_failures(tmp_path):
    frame = daily_frame(["INFY.NS"])
    record_history(parse_daily_frame(frame, {"NSE:EQ:INFY": "INFY.NS"}, settled_before=TODAY),
                   tape=TapeWriter(tmp_path), recorded_on=TODAY)  # fmt: skip
    clock = ReplayClock(T0)
    sink = RecordingSink(clock)
    quotes = TapeQuoteSource([q(INFY.key, 101.0, 1)], clock=clock)
    service = MarketService(instruments={INFY.key: INFY}, quotes=quotes,
                            history=TapeHistorySource(read_bars(tmp_path, TODAY)), sink=sink)  # fmt: skip
    await service.load_history(TODAY, date(2026, 10, 1))
    assert service.daily(INFY.key) is not None and not service.is_lagging(INFY.key)
    f = service.features(INFY.key)
    assert f is not None and f.atr_14 is not None
    assert [a.key for a in sink.payloads(Alert)] == [f"history_missing:{INDEX_KEY}"]

    seen: list[str] = []

    async def first(quote: Quote) -> None:
        seen.append("broker")
        raise RuntimeError("bug")

    async def second(quote: Quote) -> None:
        seen.append("exits")

    service.add_listener(first)
    service.add_listener(second)
    await clock.advance(120)
    got = await service.requote([INFY.key])
    assert got[INFY.key].ltp == 101.0 and seen == ["broker", "exits"]
    assert "quote_listener_failed" in [a.key for a in sink.payloads(Alert)]
    facts = service.facts(INFY)
    assert facts.quote is not None and facts.atr == Decimal(str(f.atr_14))
    assert facts.data_source is MarketDataSource.YFINANCE and facts.adv_shares == f.adv20_shares
    assert service.marks() == {INFY.key: Decimal("101.0")}
