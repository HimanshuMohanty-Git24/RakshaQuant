"""
NSE capital-market bhavcopies (UDiFF) as a point-in-time daily dataset (plan M11.3).

* **Source:** ``https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip``
  - the UDiFF "common bhavcopy" NSE publishes for every session since 8 July 2024 (verified
  2026-10-02, PROGRESS verify table). Older sessions used a different file that this does not
  read.
* **Storage:** one Parquet file per session, ``var/datasets/bhavcopy/<YYYY>/<YYYYMMDD>.parquet``,
  holding every equity-like row (series EQ, BE, BZ, SM, ST by default) exactly as published.
* **Point in time:** a symbol is in the data on the days it traded, *including* names delisted
  since - :func:`universe` over a period is free of survivorship bias. Historical **NIFTY
  constituency is not available** from these files; a "NIFTY 50 at the time" universe needs a
  separate source.
* **Prices are as traded.** Adjusted bars come from corporate actions (splits, bonuses,
  dividends) parsed from NSE's corporate-actions CSV export (:func:`parse_corporate_actions`).

Fetching is polite (a delay between requests, a browser-like User-Agent, no retries hammering)
and resumable (sessions already on disk are skipped). The owner allows downloads for research
at this rate (PROGRESS OD-10); nothing here runs on its own.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.domain.types import Bar, MarketDataSource, Timeframe

URL = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
)
FIRST_UDIFF_DAY = date(2024, 7, 8)
SERIES = ("EQ", "BE", "BZ", "SM", "ST")
REQUIRED = ("TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric",
            "TtlTradgVol")  # fmt: skip
SCHEMA = pa.schema([
    ("trade_date", pa.date32()), ("symbol", pa.string()), ("series", pa.string()),
    ("isin", pa.string()), ("open", pa.float64()), ("high", pa.float64()),
    ("low", pa.float64()), ("close", pa.float64()), ("prev_close", pa.float64()),
    ("volume", pa.int64()), ("turnover", pa.float64()), ("trades", pa.int64()),
])  # fmt: skip


def url_for(day: date) -> str:
    if day < FIRST_UDIFF_DAY:
        raise ValueError(f"{day} predates the UDiFF bhavcopy ({FIRST_UDIFF_DAY})")
    return URL.format(day=day)


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "", "-") else None
    except ValueError:
        return None


def parse_udiff(content: bytes, *, series: Sequence[str] = SERIES) -> list[dict[str, Any]]:
    """Rows of one UDiFF bhavcopy (the zip or its CSV) for the given series."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
            content = archive.read(name)
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"not a UDiFF CM bhavcopy: missing columns {missing}")
    wanted = set(series)
    rows = []
    for raw in reader:
        if (raw.get("SctySrs") or "").strip() not in wanted:
            continue
        values = [_float(raw.get(c)) for c in ("OpnPric", "HghPric", "LwPric", "ClsPric")]
        if any(v is None for v in values):
            continue
        rows.append({
            "trade_date": datetime.strptime(raw["TradDt"].strip(), "%Y-%m-%d").date(),
            "symbol": raw["TckrSymb"].strip(), "series": raw["SctySrs"].strip(),
            "isin": (raw.get("ISIN") or "").strip(),
            "open": values[0], "high": values[1], "low": values[2], "close": values[3],
            "prev_close": _float(raw.get("PrvsClsgPric")),
            "volume": int(_float(raw.get("TtlTradgVol")) or 0),
            "turnover": _float(raw.get("TtlTrfVal")),
            "trades": int(_float(raw.get("TtlNbOfTxsExctd")) or 0),
        })  # fmt: skip
    return rows


@dataclass(frozen=True)
class BhavcopyStore:
    root: Path

    def path(self, day: date) -> Path:
        return self.root / f"{day:%Y}" / f"{day:%Y%m%d}.parquet"

    def has(self, day: date) -> bool:
        return self.path(day).exists()

    def write(self, day: date, rows: Sequence[Mapping[str, Any]]) -> Path:
        path = self.path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pylist(list(rows), schema=SCHEMA), tmp)
        tmp.replace(path)
        return path

    def days(self, start: date, end: date) -> list[date]:
        found = []
        for path in sorted(self.root.glob("*/*.parquet")):
            day = datetime.strptime(path.stem, "%Y%m%d").date()
            if start <= day <= end:
                found.append(day)
        return found

    def read(
        self, start: date, end: date, *, symbols: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        wanted = set(symbols) if symbols is not None else None
        rows = []
        for day in self.days(start, end):
            for row in pq.read_table(self.path(day), schema=SCHEMA).to_pylist():
                if wanted is None or row["symbol"] in wanted:
                    rows.append(row)
        return rows


def universe(store: BhavcopyStore, start: date, end: date, *, series: Sequence[str] = ("EQ",),
             min_sessions: int = 1) -> list[str]:  # fmt: skip
    """Every symbol that traded in ``series`` during the period - delisted names included."""
    counts: dict[str, int] = {}
    for row in store.read(start, end):
        if row["series"] in series:
            counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
    return sorted(s for s, n in counts.items() if n >= min_sessions)


# -- corporate actions -------------------------------------------------------------------------


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    kind: str  # "split", "bonus", "dividend", "other"
    ratio: Decimal | None  # shares after / before (split, bonus)
    amount: Decimal | None  # per share (dividend)
    purpose: str


_SPLIT = re.compile(r"(?:split|sub-?division).*?rs\.?\s*([\d.]+).*?to\s*rs\.?\s*([\d.]+)", re.I)
_BONUS = re.compile(r"bonus\s*([\d]+)\s*:\s*([\d]+)", re.I)
_DIVIDEND = re.compile(r"dividend.*?(?:rs|re)\.?\s*([\d.]+)", re.I)


def classify(purpose: str) -> tuple[str, Decimal | None, Decimal | None]:
    if m := _SPLIT.search(purpose):  # face value 10 -> 2: five shares for one
        before, after = Decimal(m.group(1)), Decimal(m.group(2))
        return ("split", before / after, None) if after else ("other", None, None)
    if m := _BONUS.search(purpose):  # bonus a:b: a new shares for every b held
        new, held = Decimal(m.group(1)), Decimal(m.group(2))
        return ("bonus", (held + new) / held, None) if held else ("other", None, None)
    if m := _DIVIDEND.search(purpose):
        return "dividend", None, Decimal(m.group(1))
    return "other", None, None


def parse_corporate_actions(text: str) -> list[CorporateAction]:
    """NSE's corporate-actions CSV export (columns SYMBOL, PURPOSE, EX-DATE, among others)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {(f or "").strip().upper(): f for f in reader.fieldnames or []}
    symbol, purpose, ex = fields.get("SYMBOL"), fields.get("PURPOSE"), fields.get("EX-DATE")
    if not (symbol and purpose and ex):
        raise ValueError("not an NSE corporate-actions export: need SYMBOL, PURPOSE, EX-DATE")
    out = []
    for row in reader:
        text_date = (row.get(ex) or "").strip()
        try:
            ex_date = datetime.strptime(text_date, "%d-%b-%Y").date()
        except ValueError:
            continue  # "-" for actions without an ex-date
        kind, ratio, amount = classify(row.get(purpose) or "")
        out.append(CorporateAction(symbol=(row.get(symbol) or "").strip(), ex_date=ex_date,
                                   kind=kind, ratio=ratio, amount=amount,
                                   purpose=(row.get(purpose) or "").strip()))  # fmt: skip
    return out


def adjustment_factors(
    actions: Iterable[CorporateAction], closes: Mapping[date, float]
) -> dict[date, float]:
    """Backward adjustment factor per date (multiply a raw price on that date by it): splits
    and bonuses by their ratio, dividends by (1 - amount / previous close)."""
    days = sorted(closes)
    events: dict[date, float] = {}
    for a in actions:
        before = [d for d in days if d < a.ex_date]
        if not before:
            continue
        if a.kind in ("split", "bonus") and a.ratio:
            events[a.ex_date] = events.get(a.ex_date, 1.0) / float(a.ratio)
        elif a.kind == "dividend" and a.amount:
            prev = closes[before[-1]]
            if prev > float(a.amount):
                events[a.ex_date] = events.get(a.ex_date, 1.0) * (1 - float(a.amount) / prev)
    factors, running = {}, 1.0
    for day in reversed(days):  # a factor applies to every session before the ex-date
        factors[day] = running
        running *= events.get(day, 1.0)
    return factors


def to_bars(rows: Iterable[Mapping[str, Any]], *, prefix: str = "NSE:EQ:",
            actions: Sequence[CorporateAction] = ()) -> list[Bar]:  # fmt: skip
    """Raw and adjusted daily bars per symbol (series EQ rows), for the backtest."""
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["series"] == "EQ":
            by_symbol.setdefault(row["symbol"], []).append(row)
    bars = []
    for symbol, items in by_symbol.items():
        items = sorted(items, key=lambda r: r["trade_date"])
        closes = {r["trade_date"]: r["close"] for r in items}
        factors = adjustment_factors([a for a in actions if a.symbol == symbol], closes)
        for r in items:
            f = factors.get(r["trade_date"], 1.0)
            for adjusted, k in ((False, 1.0), (True, f)):
                bars.append(Bar(instrument_key=f"{prefix}{symbol}", timeframe=Timeframe.D1,
                                session_date=r["trade_date"], open=r["open"] * k,
                                high=r["high"] * k, low=r["low"] * k, close=r["close"] * k,
                                volume=int(r["volume"]), is_settled=True, adjusted=adjusted,
                                source=MarketDataSource.REPLAY))  # fmt: skip
    return bars


def load_corporate_actions(path: Path) -> list[CorporateAction]:
    """The table ``scripts/fetch_bhavcopy.py --corporate-actions`` wrote (none if absent)."""
    if not path.exists():
        return []
    return [CorporateAction(symbol=r["symbol"], ex_date=r["ex_date"], kind=r["kind"],
                            ratio=Decimal(r["ratio"]) if r["ratio"] else None,
                            amount=Decimal(r["amount"]) if r["amount"] else None,
                            purpose=r["purpose"])
            for r in pq.read_table(path).to_pylist()]  # fmt: skip


def dataset_bars(
    store: BhavcopyStore,
    start: date,
    end: date,
    *,
    symbols: Sequence[str] | None = None,
    warmup_days: int = 500,
    actions: Sequence[CorporateAction] = (),
) -> tuple[list[str], list[Bar]]:
    """The universe (every EQ symbol that traded in the period when ``symbols`` is None -
    delisted names included) and its bars from ``start - warmup_days`` to ``end``."""
    chosen = list(symbols) if symbols is not None else universe(store, start, end)
    rows = store.read(start - timedelta(days=warmup_days), end, symbols=chosen)
    return chosen, to_bars(rows, actions=actions)
