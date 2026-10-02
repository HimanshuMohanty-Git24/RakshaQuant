"""Plan M11.3: the UDiFF bhavcopy dataset - parsing, storage, a survivorship-free universe and
adjusted bars from corporate actions. No network: the files are built here."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from src.marketdata.bhavcopy import (
    BhavcopyStore,
    adjustment_factors,
    classify,
    parse_corporate_actions,
    parse_udiff,
    to_bars,
    universe,
    url_for,
)

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
          "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
          "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
          "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")  # fmt: skip


def row(day: str, symbol: str, series: str, o: float, h: float, lo: float, c: float, v: int) -> str:
    return (f"{day},{day},CM,NSE,STK,1594,INE009A01021,{symbol},{series},,,,,{symbol} LTD,"
            f"{o},{h},{lo},{c},{c},{o},,{c},,,{v},{v * c},1234,F1,1,,,,,")  # fmt: skip


def bhavcopy(day: str, rows: list[str], *, zipped: bool = True) -> bytes:
    text = "\n".join([HEADER, *rows]) + "\n"
    if not zipped:
        return text.encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"BhavCopy_NSE_CM_0_0_0_{day.replace('-', '')}_F_0000.csv", text)
    return buffer.getvalue()


def test_the_url_and_its_first_day():
    assert url_for(date(2026, 10, 5)) == ("https://nsearchives.nseindia.com/content/cm/"
                                          "BhavCopy_NSE_CM_0_0_0_20261005_F_0000.csv.zip")  # fmt: skip
    with pytest.raises(ValueError, match="predates"):
        url_for(date(2024, 7, 5))


def test_a_udiff_file_parses_and_keeps_equity_series_only():
    content = bhavcopy("2026-10-05", [row("2026-10-05", "INFY", "EQ", 1500, 1520, 1490, 1510, 900000),
                                      row("2026-10-05", "INFY", "N1", 100, 100, 100, 100, 1),
                                      row("2026-10-05", "SMALLCO", "BE", 50, 52, 49, 51, 1000)])  # fmt: skip
    rows = parse_udiff(content)
    assert [(r["symbol"], r["series"]) for r in rows] == [("INFY", "EQ"), ("SMALLCO", "BE")]
    assert rows[0]["close"] == 1510.0 and rows[0]["prev_close"] == 1500.0
    assert rows[0]["trade_date"] == date(2026, 10, 5) and rows[0]["isin"] == "INE009A01021"
    assert parse_udiff(bhavcopy("2026-10-05", [], zipped=False)) == []
    with pytest.raises(ValueError, match="missing columns"):
        parse_udiff(b"SYMBOL,CLOSE\nINFY,1\n")


def test_the_universe_includes_names_that_stopped_trading(tmp_path):
    store = BhavcopyStore(tmp_path)
    store.write(date(2026, 9, 30), parse_udiff(bhavcopy("2026-09-30", [
        row("2026-09-30", "INFY", "EQ", 1, 1, 1, 1, 10), row("2026-09-30", "GONECO", "EQ", 5, 5, 5, 5, 10)])))  # fmt: skip
    store.write(date(2026, 10, 1), parse_udiff(bhavcopy("2026-10-01", [
        row("2026-10-01", "INFY", "EQ", 1, 1, 1, 1, 10)])))  # GONECO delisted  # fmt: skip
    assert store.days(date(2026, 9, 1), date(2026, 10, 31)) == [
        date(2026, 9, 30),
        date(2026, 10, 1),
    ]
    assert universe(store, date(2026, 9, 1), date(2026, 10, 31)) == ["GONECO", "INFY"]
    assert universe(store, date(2026, 10, 1), date(2026, 10, 31)) == ["INFY"]  # point in time
    assert len(store.read(date(2026, 9, 1), date(2026, 10, 31), symbols=["INFY"])) == 2


def test_corporate_actions_adjust_history():
    assert classify("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share")[
        :2
    ] == ("split", Decimal(5))
    assert classify("Bonus 1:1")[:2] == ("bonus", Decimal(2))
    assert classify("Interim Dividend - Rs 21 Per Share") == ("dividend", None, Decimal(21))
    assert classify("Annual General Meeting")[0] == "other"
    csv_text = ("SYMBOL,COMPANY NAME,SERIES,PURPOSE,FACE VALUE,EX-DATE,RECORD DATE\n"
                "XYZ,XYZ LTD,EQ,Bonus 1:1,10,02-Oct-2026,02-Oct-2026\n"
                "XYZ,XYZ LTD,EQ,AGM,10,-,-\n")  # fmt: skip
    actions = parse_corporate_actions(csv_text)
    assert [(a.symbol, a.kind, a.ex_date) for a in actions] == [("XYZ", "bonus", date(2026, 10, 2))]
    closes = {date(2026, 9, 30): 200.0, date(2026, 10, 1): 210.0, date(2026, 10, 2): 105.0}
    factors = adjustment_factors(actions, closes)
    assert factors == {date(2026, 9, 30): 0.5, date(2026, 10, 1): 0.5, date(2026, 10, 2): 1.0}
    rows = [{"trade_date": d, "symbol": "XYZ", "series": "EQ", "open": c, "high": c, "low": c,
             "close": c, "volume": 1000} for d, c in closes.items()]  # fmt: skip
    bars = {(b.session_date, b.adjusted): b.close for b in to_bars(rows, actions=actions)}
    assert bars[(date(2026, 10, 1), False)] == 210.0 and bars[(date(2026, 10, 1), True)] == 105.0
    assert bars[(date(2026, 10, 2), True)] == 105.0  # continuous across the bonus


def test_a_point_in_time_dataset_for_the_backtest(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.marketdata.bhavcopy import dataset_bars, load_corporate_actions

    store = BhavcopyStore(tmp_path / "bhavcopy")
    for day, names in ((date(2026, 9, 30), ["INFY", "GONECO"]), (date(2026, 10, 1), ["INFY"])):
        lines = [row(day.isoformat(), n, "EQ", 10, 11, 9, 10.5, 100) for n in names]
        store.write(day, parse_udiff(bhavcopy(day.isoformat(), lines)))
    chosen, bars = dataset_bars(store, date(2026, 9, 30), date(2026, 10, 1))
    assert chosen == ["GONECO", "INFY"]  # the delisted name is in the universe
    assert {b.instrument_key for b in bars} == {"NSE:EQ:GONECO", "NSE:EQ:INFY"}
    assert len(bars) == 6  # raw and adjusted for three symbol-days
    path = tmp_path / "corporate_actions.parquet"
    assert load_corporate_actions(path) == []
    pq.write_table(pa.Table.from_pylist([{"symbol": "INFY", "ex_date": date(2026, 10, 1),
                                          "kind": "bonus", "ratio": "2", "amount": None,
                                          "purpose": "Bonus 1:1"}]), path)  # fmt: skip
    (action,) = load_corporate_actions(path)
    assert action.kind == "bonus" and action.ratio == Decimal(2)
