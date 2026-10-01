"""Plan M1.5: the Parquet tape round-trips quotes and bars and survives a crash mid-compaction."""

import shutil
from datetime import UTC, date, datetime, timedelta

from src.domain.types import Bar, MarketDataSource, Quote, Timeframe
from src.store.tape import TapeWriter, day_dir, read_bars, read_quotes

DAY = date(2026, 10, 5)
T0 = datetime(2026, 10, 5, 3, 50, tzinfo=UTC)  # 09:20 IST


def _quote(i: int, key: str = "NSE:EQ:INFY", received: datetime = T0) -> Quote:
    return Quote(
        instrument_key=key,
        ltp=1500.0 + i,
        bid=1499.9 + i if i % 2 else None,
        prev_close=1498.0,
        volume_cum=1000 * i,
        exchange_ts=received - timedelta(minutes=15),
        receipt_ts=received + timedelta(seconds=i),
        source=MarketDataSource.YFINANCE,
        is_delayed=True,
        quality_flags=("delayed",) if i % 3 == 0 else (),
    )


def _bar(day: date) -> Bar:
    return Bar(
        instrument_key="NSE:EQ:INFY",
        timeframe=Timeframe.D1,
        session_date=day,
        open=1490.0,
        high=1520.0,
        low=1485.0,
        close=1498.0,
        volume=4_200_000,
        is_settled=True,
        source=MarketDataSource.YFINANCE,
    )


def test_quotes_round_trip_in_recorded_order(tmp_path):
    quotes = [_quote(i) for i in range(5)]
    writer = TapeWriter(tmp_path)
    writer.add_quotes(quotes)
    assert writer.pending == 5
    (path,) = writer.flush()
    assert path.parent == day_dir(tmp_path, DAY) and writer.pending == 0
    assert read_quotes(tmp_path, DAY) == quotes


def test_bars_round_trip_under_their_recording_day(tmp_path):
    bars = [_bar(date(2026, 9, 30)), _bar(date(2026, 10, 1))]
    writer = TapeWriter(tmp_path)
    writer.add_bars(bars, recorded_on=DAY)
    writer.flush()
    assert read_bars(tmp_path, DAY) == bars


def test_quotes_are_partitioned_by_ist_receipt_date(tmp_path):
    late_utc = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)  # 01:30 IST on the 6th
    writer = TapeWriter(tmp_path)
    writer.add_quotes([_quote(1), _quote(2, received=late_utc)])
    writer.flush()
    assert len(read_quotes(tmp_path, DAY)) == 1
    assert len(read_quotes(tmp_path, date(2026, 10, 6))) == 1


def test_compaction_merges_parts_and_numbering_continues(tmp_path):
    writer = TapeWriter(tmp_path)
    expected: list[Quote] = []
    for batch in range(3):
        quotes = [_quote(10 * batch + i) for i in range(3)]
        expected += quotes
        writer.add_quotes(quotes)
        writer.flush()
    directory = day_dir(tmp_path, DAY)
    assert len(list(directory.glob("quotes.part-*.parquet"))) == 3

    (compacted,) = writer.compact(DAY)
    assert compacted.name == "quotes.parquet"
    assert not list(directory.glob("quotes.part-*.parquet"))
    assert read_quotes(tmp_path, DAY) == expected

    later = [_quote(99)]
    writer.add_quotes(later)
    (path,) = writer.flush()
    assert path.name == "quotes.part-000004.parquet"
    assert read_quotes(tmp_path, DAY) == expected + later


def test_parts_left_behind_by_a_crash_are_not_read_twice(tmp_path):
    writer = TapeWriter(tmp_path)
    quotes = [_quote(i) for i in range(4)]
    writer.add_quotes(quotes[:2])
    writer.flush()
    writer.add_quotes(quotes[2:])
    writer.flush()
    directory = day_dir(tmp_path, DAY)
    backup = tmp_path / "backup"
    shutil.copytree(directory, backup)
    writer.compact(DAY)
    for part in backup.glob("quotes.part-*.parquet"):  # crash: parts not deleted
        shutil.copy(part, directory / part.name)
    assert read_quotes(tmp_path, DAY) == quotes
    writer.add_quotes([_quote(7)])
    (path,) = writer.flush()
    assert path.name == "quotes.part-000003.parquet"
    assert read_quotes(tmp_path, DAY) == [*quotes, _quote(7)]


def test_missing_day_reads_empty(tmp_path):
    assert read_quotes(tmp_path, DAY) == []
    assert read_bars(tmp_path, DAY) == []
    assert TapeWriter(tmp_path).flush() == []
    assert TapeWriter(tmp_path).compact(DAY) == []
