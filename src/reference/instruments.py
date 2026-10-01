"""
Instrument master (plan M2.4) from Dhan's scrip master (verified 2026-10-02):
``https://images.dhan.co/api-data/api-scrip-master.csv`` (~25 MB, ~200k rows). NSE cash equities
are rows with ``SEM_EXM_EXCH_ID=NSE``, ``SEM_SEGMENT=E``, ``SEM_INSTRUMENT_NAME=EQUITY``.

**``SEM_TICK_SIZE`` is in paise**, not rupees: INFY 5.0 = Rs 0.05, RELIANCE 10.0 = Rs 0.10,
TATASTEEL 1.0 = Rs 0.01 (NSE's 2025 tick revision). Only the NSE equity rows are kept on disk.
A universe symbol missing from the master falls back to a Rs 0.05 tick and no broker token.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx2

from src.domain.types import Instrument
from src.reference.bands import BandTable, band_for
from src.reference.download import Snapshot, fetch_snapshot
from src.reference.universe import UniverseMember

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_MASTER_NAME = "dhan_nse_equity"
FALLBACK_TICK = Decimal("0.05")
_PAISE = Decimal(100)


@dataclass(frozen=True, slots=True)
class MasterRow:
    symbol: str
    series: str
    security_id: str
    tick_size: Decimal  # rupees
    lot_size: int
    name: str


def filter_nse_equity(body: bytes) -> bytes:
    """Keep the header and NSE cash-equity rows of the full scrip master."""
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames or [], lineterminator="\n")
    writer.writeheader()
    for row in reader:
        if (row.get("SEM_EXM_EXCH_ID"), row.get("SEM_SEGMENT"), row.get("SEM_INSTRUMENT_NAME")) == (
            "NSE",
            "E",
            "EQUITY",
        ):
            writer.writerow(row)
    return out.getvalue().encode("utf-8")


def parse_master(body: bytes) -> dict[tuple[str, str], MasterRow]:
    """``(symbol, series) -> MasterRow`` from an (already filtered) NSE equity master."""
    rows: dict[tuple[str, str], MasterRow] = {}
    for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig"))):
        try:
            tick = Decimal(row["SEM_TICK_SIZE"].strip()) / _PAISE
            lot = int(float(row["SEM_LOT_UNITS"]))
        except (InvalidOperation, ValueError, KeyError):
            continue  # unusable row: the instrument falls back below
        if tick <= 0 or lot <= 0:
            continue
        key = (row["SEM_TRADING_SYMBOL"].strip(), row["SEM_SERIES"].strip())
        rows[key] = MasterRow(
            symbol=key[0],
            series=key[1],
            security_id=row["SEM_SMST_SECURITY_ID"].strip(),
            tick_size=tick.normalize(),
            lot_size=lot,
            name=row.get("SM_SYMBOL_NAME", "").strip(),
        )
    if not rows:
        raise ValueError("no NSE equity rows in the scrip master")
    return rows


async def fetch_master(
    reference_dir: Path, day: date, *, client: httpx2.AsyncClient | None = None
) -> tuple[dict[tuple[str, str], MasterRow], Snapshot]:
    snapshot = await fetch_snapshot(
        SCRIP_MASTER_URL,
        directory=reference_dir,
        name=SCRIP_MASTER_NAME,
        ext="csv",
        day=day,
        client=client,
        transform=filter_nse_equity,
        validate=parse_master,
    )
    return parse_master(snapshot.path.read_bytes()), snapshot


@dataclass(frozen=True, slots=True)
class InstrumentSet:
    by_symbol: dict[str, Instrument]
    unmapped: tuple[str, ...]  # universe symbols absent from the master (fallback tick, no token)


def build_instruments(
    members: list[UniverseMember],
    master: dict[tuple[str, str], MasterRow] | None,
    bands: BandTable | None = None,
) -> InstrumentSet:
    """One ``Instrument`` per universe member, enriched from the master and the band file."""
    by_symbol: dict[str, Instrument] = {}
    unmapped: list[str] = []
    for m in members:
        row = None if master is None else master.get((m.symbol, m.series))
        if row is None:
            unmapped.append(m.symbol)
        by_symbol[m.symbol] = Instrument.nse_equity(
            m.symbol,
            series=m.series,
            isin=m.isin,
            name=m.company,
            sector=m.industry,
            tick_size=row.tick_size if row else FALLBACK_TICK,
            lot_size=row.lot_size if row else 1,
            broker_tokens={"dhan": row.security_id} if row else {},
            band_pct=band_for(m.symbol, m.series, bands),
        )
    return InstrumentSet(by_symbol=by_symbol, unmapped=tuple(unmapped))
