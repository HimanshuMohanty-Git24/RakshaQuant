"""
The pre-open reference refresh (plan M2.4/M2.5): universe, instrument master and price bands.

* The universe is **pinned** to a snapshot date for the experiment (plan D13): pass
  ``universe_day`` to load that snapshot, or omit it to use today's.
* The instrument master and bands are refreshed daily; when today's download fails the newest
  cached copy is used (stale). Bands are optional (defaults apply); a missing master falls back
  to a Rs 0.05 tick for every instrument. Every degradation is reported for an alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx2

from src.domain.events import Alert
from src.domain.sink import EventSink
from src.reference.bands import BandTable, fetch_bands
from src.reference.download import ReferenceDataError, Snapshot, snapshot_path
from src.reference.instruments import InstrumentSet, build_instruments, fetch_master
from src.reference.universe import NIFTY50_NAME, UniverseMember, fetch_universe, load_universe


@dataclass(frozen=True)
class ReferenceData:
    universe: list[UniverseMember]
    universe_day: date
    instruments: InstrumentSet
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)  # alert key -> message


async def refresh_reference(
    reference_dir: Path,
    day: date,
    *,
    universe_day: date | None = None,
    client: httpx2.AsyncClient | None = None,
) -> ReferenceData:
    """Load the pinned (or today's) universe and today's master and bands.

    Raises :class:`ReferenceDataError` only when no universe is available at all.
    """
    problems: dict[str, str] = {}
    snapshots: dict[str, Snapshot] = {}

    if universe_day is not None:
        pinned = snapshot_path(reference_dir, NIFTY50_NAME, universe_day, "csv")
        if not pinned.exists():
            raise ReferenceDataError(f"pinned universe snapshot {pinned.name} is missing")
        universe, used_day = load_universe(pinned), universe_day
    else:
        universe, snap = await fetch_universe(reference_dir, day, client=client)
        snapshots["universe"], used_day = snap, snap.day
        if not snap.fresh:
            problems["reference_stale:universe"] = (
                f"universe from {snap.day} (today's download failed: {snap.error})"
            )

    master = None
    try:
        master, snap = await fetch_master(reference_dir, day, client=client)
        snapshots["instrument_master"] = snap
        if not snap.fresh:
            problems["reference_stale:instrument_master"] = (
                f"instrument master from {snap.day} (today's download failed: {snap.error})"
            )
    except ReferenceDataError as exc:
        problems["reference_missing:instrument_master"] = f"{exc}; using a Rs 0.05 tick for all"

    bands: BandTable | None = None
    try:
        bands, snap = await fetch_bands(reference_dir, day, client=client)
        snapshots["bands"] = snap
        if not snap.fresh:
            problems["reference_stale:bands"] = f"price bands from {snap.day} ({snap.error})"
    except ReferenceDataError as exc:
        problems["reference_missing:bands"] = f"{exc}; using per-series default bands"

    instruments = build_instruments(universe, master, bands)
    if instruments.unmapped:
        problems["reference_unmapped"] = (
            f"{len(instruments.unmapped)} symbols missing from the instrument master "
            f"(fallback tick, no broker token): {', '.join(instruments.unmapped)}"
        )
    return ReferenceData(universe, used_day, instruments, snapshots, problems)


def alert_reference(data: ReferenceData, sink: EventSink) -> None:
    for key, message in sorted(data.problems.items()):
        sink.emit(Alert(level="WARNING", key=key, message=message), source="reference")
