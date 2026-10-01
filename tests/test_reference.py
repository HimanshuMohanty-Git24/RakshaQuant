"""Plan M2.4: universe, instrument master (paise ticks), price bands and the pre-open refresh."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from src.domain.clock import ReplayClock
from src.domain.events import Alert
from src.domain.sink import RecordingSink
from src.marketdata.validation import DEFAULT_BAND_PCT, band_lookup
from src.reference.bands import BANDS_URL, band_for, parse_bands
from src.reference.download import ReferenceDataError, fetch_snapshot, latest_snapshot
from src.reference.instruments import (
    FALLBACK_TICK,
    SCRIP_MASTER_URL,
    build_instruments,
    filter_nse_equity,
    parse_master,
)
from src.reference.refresh import alert_reference, refresh_reference
from src.reference.universe import NIFTY50_URL, parse_universe, sector_map

DAY = date(2026, 10, 5)
SYMBOLS = ["INFY", "RELIANCE", "TATASTEEL"] + [f"SYM{n:02d}" for n in range(47)]


def universe_csv(symbols: list[str] = SYMBOLS) -> bytes:
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for n, s in enumerate(symbols):
        industry = "Information Technology" if s == "INFY" else "Metals & Mining"
        rows.append(f"{s} Ltd.,{industry},{s},EQ,INE{n:06d}010")
    return ("\n".join(rows) + "\n").encode()


MASTER_CSV = (
    b"SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
    b"SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
    b"SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,"
    b"SM_SYMBOL_NAME\n"
    b"BSE,C,1026077,FUTCUR,0,USDINR-28Aug2024-FUT,1.0,USDINR AUG FUT,2024-08-28,-0.01,XX,"
    b"0.2500,M,FUTCUR,,USDINR\n"
    b"NSE,E,1594,EQUITY,,INFY,1.0,Infosys,,,,5.0000,,ES,EQ,INFOSYS LIMITED\n"
    b"NSE,E,2885,EQUITY,,RELIANCE,1.0,Reliance,,,,10.0000,,ES,EQ,RELIANCE INDUSTRIES LTD\n"
    b"NSE,E,3499,EQUITY,,TATASTEEL,1.0,Tata Steel,,,,1.0000,,ES,EQ,TATA STEEL LIMITED\n"
    b"NSE,E,9999,EQUITY,,BROKEN,x,Broken,,,,abc,,ES,EQ,BROKEN ROW\n"
    b"NSE,D,35000,FUTSTK,,INFY-FUT,400,INFY FUT,,,,5.0000,,FUT,,INFY\n"
)

BANDS_CSV = (
    b"Symbol,Series,Security Name,Band,Remarks\n"
    b'INFY,EQ,INFOSYS LIMITED,No Band,"-"\n'
    b'RELIANCE,EQ,RELIANCE INDUSTRIES LIMITED,No Band,"-"\n'
    b'TATASTEEL,EQ,TATA STEEL LIMITED,No Band,"-"\n'
    b'SMALLCO,EQ,SMALL CO LIMITED,5,"-"\n'
)


def mock_client(
    routes: dict[str, bytes | int], calls: list[str] | None = None
) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        body = routes.get(url, 404)
        if isinstance(body, int):
            return httpx2.Response(body, text="<html>error</html>")
        return httpx2.Response(200, content=body)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


ALL_ROUTES: dict[str, bytes | int] = {
    NIFTY50_URL: universe_csv(),
    SCRIP_MASTER_URL: MASTER_CSV,
    BANDS_URL: BANDS_CSV,
}


# --- universe -------------------------------------------------------------------------------


def test_universe_parses_and_maps_sectors():
    members = parse_universe(universe_csv())
    assert len(members) == 50 and members[0].symbol == "INFY"
    assert sector_map(members)["INFY"] == "Information Technology"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (universe_csv(SYMBOLS[:49]), "expected 50"),
        (universe_csv(SYMBOLS[:49] + ["INFY"]), "duplicate"),
        (universe_csv().replace(b"INE000001010", b"BAD"), "bad symbol, ISIN"),
        (b"Symbol,Series\nINFY,EQ\n", "lacks columns"),
    ],
)
def test_universe_validation(body, error):
    with pytest.raises(ValueError, match=error):
        parse_universe(body)


# --- instrument master --------------------------------------------------------------------------


def test_master_keeps_only_nse_equities_and_converts_paise_to_rupees():
    filtered = filter_nse_equity(MASTER_CSV)
    assert b"USDINR" not in filtered and b"INFY-FUT" not in filtered
    master = parse_master(filtered)
    assert master[("INFY", "EQ")].tick_size == Decimal("0.05")
    assert master[("RELIANCE", "EQ")].tick_size == Decimal("0.1")
    assert master[("TATASTEEL", "EQ")].tick_size == Decimal("0.01")
    assert master[("RELIANCE", "EQ")].security_id == "2885"
    assert ("BROKEN", "EQ") not in master  # unusable rows are skipped


def test_instruments_are_built_from_universe_master_and_bands():
    members = parse_universe(universe_csv())
    master = parse_master(filter_nse_equity(MASTER_CSV))
    built = build_instruments(members, master, parse_bands(BANDS_CSV))
    infy = built.by_symbol["INFY"]
    assert infy.key == "NSE:EQ:INFY" and infy.isin == "INE000000010"
    assert infy.sector == "Information Technology" and infy.name == "INFY Ltd."
    assert infy.tick_size == Decimal("0.05") and infy.broker_tokens == {"dhan": "1594"}
    assert infy.band_pct is None  # F&O: dynamic band
    other = built.by_symbol["SYM00"]
    assert other.tick_size == FALLBACK_TICK and other.broker_tokens == {}
    assert len(built.unmapped) == 47


# --- bands ----------------------------------------------------------------------------------------


def test_bands_parse_no_band_as_none_and_fall_back_by_series():
    table = parse_bands(BANDS_CSV)
    assert table[("INFY", "EQ")] is None and table[("SMALLCO", "EQ")] == 5.0
    assert band_for("UNKNOWN", "BE", table) == 5.0
    assert band_for("UNKNOWN", "EQ", None) is None


def test_validator_band_lookup_uses_the_default_for_dynamic_bands():
    built = build_instruments(parse_universe(universe_csv()), None, parse_bands(BANDS_CSV))
    lookup = band_lookup(built.by_symbol.values())
    assert lookup("NSE:EQ:INFY") == DEFAULT_BAND_PCT
    small = build_instruments(
        parse_universe(universe_csv(["SMALLCO"] + SYMBOLS[1:])), None, parse_bands(BANDS_CSV)
    )
    assert band_lookup(small.by_symbol.values())("NSE:EQ:SMALLCO") == 5.0


# --- dated cache ------------------------------------------------------------------------------------


async def test_snapshots_are_downloaded_once_per_day(tmp_path: Path):
    calls: list[str] = []
    async with mock_client(ALL_ROUTES, calls) as client:
        first = await fetch_snapshot(NIFTY50_URL, directory=tmp_path, name="nifty50", ext="csv",
                                     day=DAY, client=client, validate=parse_universe)  # fmt: skip
        again = await fetch_snapshot(NIFTY50_URL, directory=tmp_path, name="nifty50", ext="csv",
                                     day=DAY, client=client, validate=parse_universe)  # fmt: skip
    assert first.fresh and again.fresh and first.path == again.path
    assert first.path.name == "nifty50_2026-10-05.csv" and len(calls) == 1
    assert not list(tmp_path.glob("*.tmp"))


async def test_failed_download_falls_back_to_the_newest_older_snapshot(tmp_path: Path):
    (tmp_path / "nifty50_2026-09-28.csv").write_bytes(universe_csv())
    (tmp_path / "nifty50_2026-10-01.csv").write_bytes(universe_csv())
    (tmp_path / "nifty50_2026-10-09.csv").write_bytes(universe_csv())  # future: never used
    async with mock_client({NIFTY50_URL: 503}) as client:
        snap = await fetch_snapshot(NIFTY50_URL, directory=tmp_path, name="nifty50", ext="csv",
                                    day=DAY, client=client, validate=parse_universe)  # fmt: skip
    assert not snap.fresh and snap.day == date(2026, 10, 1) and "503" in (snap.error or "")
    assert latest_snapshot(tmp_path, "nifty50", "csv", DAY) == (date(2026, 10, 1), snap.path)


async def test_an_invalid_body_counts_as_a_failed_download(tmp_path: Path):
    async with mock_client({NIFTY50_URL: b"<html>maintenance</html>"}) as client:
        with pytest.raises(ReferenceDataError, match="no cached copy"):
            await fetch_snapshot(NIFTY50_URL, directory=tmp_path, name="nifty50", ext="csv",
                                 day=DAY, client=client, validate=parse_universe)  # fmt: skip
    assert not list(tmp_path.iterdir())  # nothing half-written


# --- the pre-open refresh -------------------------------------------------------------------------


async def test_refresh_builds_instruments_and_reports_nothing_when_healthy(tmp_path: Path):
    async with mock_client(ALL_ROUTES) as client:
        data = await refresh_reference(tmp_path, DAY, client=client)
    assert data.universe_day == DAY and len(data.instruments.by_symbol) == 50
    assert set(data.snapshots) == {"universe", "instrument_master", "bands"}
    assert set(data.problems) == {"reference_unmapped"}  # the 47 synthetic symbols
    stored = (tmp_path / "dhan_nse_equity_2026-10-05.csv").read_bytes()
    assert b"USDINR" not in stored  # only NSE equities are kept on disk


async def test_refresh_degrades_and_alerts_when_master_and_bands_are_down(tmp_path: Path):
    routes: dict[str, bytes | int] = {
        NIFTY50_URL: universe_csv(),
        SCRIP_MASTER_URL: 500,
        BANDS_URL: 500,
    }
    async with mock_client(routes) as client:
        data = await refresh_reference(tmp_path, DAY, client=client)
    assert all(i.tick_size == FALLBACK_TICK for i in data.instruments.by_symbol.values())
    assert {"reference_missing:instrument_master", "reference_missing:bands"} <= set(data.problems)
    sink = RecordingSink(ReplayClock(datetime(2026, 10, 5, 3, 0, tzinfo=UTC)))
    alert_reference(data, sink)
    assert {a.key for a in sink.payloads(Alert)} == set(data.problems)


async def test_pinned_universe_is_used_and_must_exist(tmp_path: Path):
    (tmp_path / "nifty50_2026-10-01.csv").write_bytes(universe_csv())
    async with mock_client(ALL_ROUTES) as client:
        data = await refresh_reference(tmp_path, DAY, universe_day=date(2026, 10, 1), client=client)
        assert data.universe_day == date(2026, 10, 1) and "universe" not in data.snapshots
        with pytest.raises(ReferenceDataError, match="pinned universe"):
            await refresh_reference(tmp_path, DAY, universe_day=date(2026, 9, 1), client=client)
