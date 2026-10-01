"""
Dated, cached downloads of reference files into ``var/reference/``.

Each file is fetched at most once per day (``<name>_<YYYY-MM-DD>.<ext>``), written atomically.
If today's download fails, the newest earlier snapshot is used and the caller is told it is
stale; with no snapshot at all the caller gets a :class:`ReferenceDataError`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx2

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RakshaQuant/0.1 (paper trading research)"
TIMEOUT_S = 60.0

_DATED = re.compile(r"^(?P<name>.+)_(?P<day>\d{4}-\d{2}-\d{2})\.(?P<ext>[a-z]+)$")


class ReferenceDataError(RuntimeError):
    """No usable copy of a reference file (download failed and nothing cached)."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    day: date
    fresh: bool  # downloaded (or already cached) for the requested day
    error: str | None = None  # why today's download failed, when stale


def snapshot_path(directory: Path, name: str, day: date, ext: str) -> Path:
    return directory / f"{name}_{day.isoformat()}.{ext}"


def latest_snapshot(
    directory: Path, name: str, ext: str, on_or_before: date
) -> tuple[date, Path] | None:
    """The newest ``(day, path)`` snapshot dated on or before ``on_or_before``."""
    best: tuple[date, Path] | None = None
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        match = _DATED.match(path.name)
        if match is None or match["name"] != name or match["ext"] != ext:
            continue
        day = date.fromisoformat(match["day"])
        if day <= on_or_before and (best is None or day > best[0]):
            best = (day, path)
    return best


async def fetch_snapshot(
    url: str,
    *,
    directory: Path,
    name: str,
    ext: str,
    day: date,
    client: httpx2.AsyncClient | None = None,
    transform: Callable[[bytes], bytes] | None = None,
    validate: Callable[[bytes], object] | None = None,
) -> Snapshot:
    """Today's copy if cached, else download it; on failure fall back to the newest older copy.

    ``transform`` reduces the body before it is stored (e.g. keep only NSE equities);
    ``validate`` raises if the (transformed) body is unusable, which counts as a failure.
    """
    target = snapshot_path(directory, name, day, ext)
    if target.exists():
        return Snapshot(target, day, fresh=True)
    try:
        body = await _get(url, client)
        if transform is not None:
            body = transform(body)
        if validate is not None:
            validate(body)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, target)
        return Snapshot(target, day, fresh=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        fallback = latest_snapshot(directory, name, ext, day)
        if fallback is None:
            raise ReferenceDataError(
                f"{name}: download failed ({error}) and no cached copy"
            ) from exc
        return Snapshot(fallback[1], fallback[0], fresh=False, error=error)


async def _get(url: str, client: httpx2.AsyncClient | None) -> bytes:
    if client is not None:
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return response.content
    async with httpx2.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True) as own:
        return await _get(url, own)
