"""
Execution-core benchmarks (plan §8, M3; audit §D.2/§D.3). Run with ``pytest -m bench``.

Budgets (audit): paper submit + persist < 50 ms; ``place_order`` at 5,000 orders <= 5 ms (the
legacy engine took 102 ms there because it rewrote its whole JSON state on every fill).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.domain.events import Heartbeat, make_event
from src.domain.types import Side
from src.store.event_store import EventStore
from tests.oms_harness import harness, intent

pytestmark = pytest.mark.bench

SUBMIT_BUDGET_S = 0.005
APPEND_BUDGET_S = 0.005


def _median(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def _time(fn: Callable[[], Any], runs: int) -> list[float]:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return samples


def test_bench_oms_submit_with_5000_prior_orders(tmp_path: Path) -> None:
    async def run() -> list[float]:
        with harness(tmp_path, cash="1000000000") as h:
            await h.oms.start()
            h.quote(1000.0)
            for n in range(5000):  # 5,000 prior orders, each filled
                await h.oms.submit(intent(Side.BUY, 1, leg=f"seed-{n}"))
            h.quote(1000.0, volume=10**9)
            samples = []
            for n in range(50):
                started = time.perf_counter()
                await h.oms.submit(intent(Side.BUY, 1, leg=f"timed-{n}"))
                samples.append(time.perf_counter() - started)
            return samples

    samples = asyncio.run(run())
    median = _median(samples)
    print(f"\nOMS.submit (event + broker + state) at 5,000 orders: median {median * 1000:.2f} ms")
    assert median <= SUBMIT_BUDGET_S, f"submit took {median * 1000:.1f} ms (budget 5 ms)"


def test_bench_event_append_with_projections(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    with EventStore(tmp_path / "rq.db") as store:
        ts = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
        for n in range(10_000):
            store.append(make_event(Heartbeat(pid=1, uptime_s=n), ts=ts, source="bench"))
        samples = _time(
            lambda: store.append(make_event(Heartbeat(pid=1, uptime_s=0), ts=ts, source="bench")),
            200,
        )
    median = _median(samples)
    print(f"\nEventStore.append at 10,000 events: median {median * 1000:.3f} ms")
    assert median <= APPEND_BUDGET_S
