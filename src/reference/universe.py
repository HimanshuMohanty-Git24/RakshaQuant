"""
The trading universe (plan D13, M2.4): the NIFTY 50 constituents as of a snapshot date.

Source (verified 2026-10-02): ``https://niftyindices.com/IndexConstituent/ind_nifty50list.csv``
with columns ``Company Name, Industry, Symbol, Series, ISIN Code``. The ``Industry`` column is
the sector map. Snapshots are cached as ``var/reference/nifty50_<date>.csv``; the experiment pins
one date for the whole month (held symbols are added by the engine).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx2

from src.reference.download import Snapshot, fetch_snapshot

NIFTY50_URL = "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"
NIFTY50_NAME = "nifty50"
NIFTY50_SIZE = 50

_ISIN = re.compile(r"^IN[A-Z0-9]{9}\d$")
_COLUMNS = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    series: str
    isin: str
    company: str
    industry: str


def parse_universe(
    body: bytes, *, expected_size: int | None = NIFTY50_SIZE
) -> list[UniverseMember]:
    """Parse and validate a niftyindices constituents CSV."""
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    missing = [c for c in _COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"constituents CSV lacks columns {missing}")
    members = [
        UniverseMember(
            symbol=row["Symbol"].strip(),
            series=row["Series"].strip(),
            isin=row["ISIN Code"].strip(),
            company=row["Company Name"].strip(),
            industry=row["Industry"].strip(),
        )
        for row in reader
    ]
    symbols = [m.symbol for m in members]
    if expected_size is not None and len(members) != expected_size:
        raise ValueError(f"expected {expected_size} constituents, got {len(members)}")
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbols in the constituents CSV")
    bad = [m.symbol for m in members if not m.symbol or not _ISIN.match(m.isin) or not m.industry]
    if bad:
        raise ValueError(f"constituents with a bad symbol, ISIN or industry: {bad}")
    return members


async def fetch_universe(
    reference_dir: Path, day: date, *, client: httpx2.AsyncClient | None = None
) -> tuple[list[UniverseMember], Snapshot]:
    """Today's NIFTY 50 snapshot (cached), or the newest valid older one if the download fails."""
    snapshot = await fetch_snapshot(
        NIFTY50_URL,
        directory=reference_dir,
        name=NIFTY50_NAME,
        ext="csv",
        day=day,
        client=client,
        validate=parse_universe,
    )
    return load_universe(snapshot.path), snapshot


def load_universe(path: Path) -> list[UniverseMember]:
    return parse_universe(path.read_bytes())


def sector_map(members: list[UniverseMember]) -> dict[str, str]:
    """Symbol -> sector (the index provider's industry classification)."""
    return {m.symbol: m.industry for m in members}
