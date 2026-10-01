"""
Parquet market tape (plan M1.5; audit §G.4): what the system *received*, kept for replay.

Layout: ``<root>/<YYYY-MM-DD>/<stream>.parquet`` with ``stream`` in {``quotes``, ``bars``}.
Quotes are partitioned by the IST date they were received; bars by the day they were recorded.

Writes are crash-safe. :meth:`TapeWriter.flush` writes each batch as a numbered part file
(temp file + ``os.replace``). :meth:`TapeWriter.compact` merges the parts into the single
``<stream>.parquet`` and stamps it with the highest part number it absorbed, so readers skip
parts that were compacted even if a crash left them behind. Rows keep their recorded order.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.domain.types import Bar, Quote
from src.utils.market_time import IST

QUOTES = "quotes"
BARS = "bars"

QUOTE_SCHEMA = pa.schema(
    [
        ("instrument_key", pa.string()),
        ("ltp", pa.float64()),
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("prev_close", pa.float64()),
        ("volume_cum", pa.int64()),
        ("exchange_ts", pa.timestamp("us", tz="UTC")),
        ("receipt_ts", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("is_delayed", pa.bool_()),
        ("quality_flags", pa.list_(pa.string())),
    ]
)

BAR_SCHEMA = pa.schema(
    [
        ("instrument_key", pa.string()),
        ("timeframe", pa.string()),
        ("session_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("is_settled", pa.bool_()),
        ("adjusted", pa.bool_()),
        ("source", pa.string()),
    ]
)

_SCHEMAS = {QUOTES: QUOTE_SCHEMA, BARS: BAR_SCHEMA}
_PART = re.compile(r"^(quotes|bars)\.part-(\d{6})\.parquet$")
_MAX_PART_KEY = b"rq_max_part"


def day_dir(root: Path, day: date) -> Path:
    return root / day.isoformat()


class TapeWriter:
    """Buffers quotes and bars and writes them to the tape. Not thread-safe."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._buffers: defaultdict[tuple[date, str], list[dict[str, Any]]] = defaultdict(list)

    @property
    def pending(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def add_quotes(self, quotes: Iterable[Quote]) -> None:
        for q in quotes:
            day = q.receipt_ts.astimezone(IST).date()
            self._buffers[(day, QUOTES)].append(_quote_row(q))

    def add_bars(self, bars: Iterable[Bar], *, recorded_on: date) -> None:
        self._buffers[(recorded_on, BARS)].extend(_bar_row(b) for b in bars)

    def flush(self) -> list[Path]:
        """Write every buffered batch as a new part file; return the paths written."""
        written: list[Path] = []
        for (day, stream), rows in sorted(self._buffers.items()):
            if not rows:
                continue
            directory = day_dir(self.root, day)
            directory.mkdir(parents=True, exist_ok=True)
            part = _next_part(directory, stream)
            path = directory / f"{stream}.part-{part:06d}.parquet"
            _write_atomic(pa.Table.from_pylist(rows, schema=_SCHEMAS[stream]), path)
            written.append(path)
        self._buffers.clear()
        return written

    def compact(self, day: date) -> list[Path]:
        """Merge each stream's parts for ``day`` into ``<stream>.parquet``; delete the parts."""
        directory = day_dir(self.root, day)
        compacted: list[Path] = []
        for stream, schema in _SCHEMAS.items():
            parts = _live_parts(directory, stream)
            if not parts:
                continue
            table = _read_stream(directory, stream, schema)
            max_part = parts[-1][0]
            meta = dict(table.schema.metadata or {})
            meta[_MAX_PART_KEY] = str(max_part).encode()
            target = directory / f"{stream}.parquet"
            _write_atomic(table.replace_schema_metadata(meta), target)
            for _, path in parts:
                path.unlink(missing_ok=True)
            compacted.append(target)
        return compacted


def read_quotes(root: Path, day: date) -> list[Quote]:
    table = _read_stream(day_dir(root, day), QUOTES, QUOTE_SCHEMA)
    return [Quote.model_validate(row) for row in table.to_pylist()]


def read_bars(root: Path, day: date) -> list[Bar]:
    table = _read_stream(day_dir(root, day), BARS, BAR_SCHEMA)
    return [Bar.model_validate(row) for row in table.to_pylist()]


# -- internals ------------------------------------------------------------------------------


def _quote_row(q: Quote) -> dict[str, Any]:
    row = q.model_dump()
    row["exchange_ts"] = q.exchange_ts.astimezone(UTC)
    row["receipt_ts"] = q.receipt_ts.astimezone(UTC)
    row["source"] = q.source.value
    row["quality_flags"] = list(q.quality_flags)
    return row


def _bar_row(b: Bar) -> dict[str, Any]:
    row = b.model_dump()
    row["timeframe"] = b.timeframe.value
    row["source"] = b.source.value
    return row


def _all_parts(directory: Path, stream: str) -> list[tuple[int, Path]]:
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = _PART.match(path.name)
        if match is not None and match.group(1) == stream:
            found.append((int(match.group(2)), path))
    return sorted(found)


def _compacted_max_part(directory: Path, stream: str) -> int:
    path = directory / f"{stream}.parquet"
    if not path.exists():
        return 0
    meta = pq.read_schema(path).metadata or {}
    return int(meta.get(_MAX_PART_KEY, b"0"))


def _live_parts(directory: Path, stream: str) -> list[tuple[int, Path]]:
    """Parts not yet absorbed by the compacted file."""
    absorbed = _compacted_max_part(directory, stream)
    return [(n, p) for n, p in _all_parts(directory, stream) if n > absorbed]


def _next_part(directory: Path, stream: str) -> int:
    parts = _all_parts(directory, stream)
    highest = parts[-1][0] if parts else 0
    return max(highest, _compacted_max_part(directory, stream)) + 1


def _read_stream(directory: Path, stream: str, schema: Any) -> Any:
    paths = []
    compacted = directory / f"{stream}.parquet"
    if compacted.exists():
        paths.append(compacted)
    paths.extend(path for _, path in _live_parts(directory, stream))
    if not paths:
        return schema.empty_table()
    # Drop file metadata (the compaction stamp) so the tables' schemas match exactly.
    return pa.concat_tables(
        pq.read_table(path, schema=schema).replace_schema_metadata(None) for path in paths
    )


def _write_atomic(table: Any, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
