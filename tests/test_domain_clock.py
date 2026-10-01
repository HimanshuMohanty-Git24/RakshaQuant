"""Plan M1.4: wall and replay clocks; replay wakes sleepers deterministically in time order."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.domain.clock import ReplayClock, WallClock, now_ist

T0 = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)  # 09:00 IST


async def test_wall_clock_is_aware_utc():
    clock = WallClock()
    assert clock.now().tzinfo is UTC
    await clock.sleep(0)
    await clock.sleep(-5)  # never negative
    await clock.sleep_until(clock.now() - timedelta(seconds=1))
    assert now_ist(clock).utcoffset() == timedelta(hours=5, minutes=30)


def test_replay_clock_needs_aware_start():
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(datetime(2026, 10, 5, 9, 0))


async def test_time_only_moves_when_advanced():
    clock = ReplayClock(T0)
    await asyncio.sleep(0)
    assert clock.now() == T0
    await clock.advance(90)
    assert clock.now() == T0 + timedelta(seconds=90)
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance_to(T0)


async def test_sleepers_wake_in_time_order_at_their_wake_time():
    clock = ReplayClock(T0)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((name, clock.now()))

    tasks = [
        asyncio.create_task(sleeper("late", 60)),
        asyncio.create_task(sleeper("early", 30)),
        asyncio.create_task(sleeper("tie-1", 45)),
        asyncio.create_task(sleeper("tie-2", 45)),
    ]
    await asyncio.sleep(0)
    assert clock.pending_sleepers == 4
    await clock.advance(50)
    assert [n for n, _ in woke] == ["early", "tie-1", "tie-2"]
    assert woke[0][1] == T0 + timedelta(seconds=30)
    assert clock.now() == T0 + timedelta(seconds=50)
    await clock.advance(10)
    assert woke[-1] == ("late", T0 + timedelta(seconds=60))
    await asyncio.gather(*tasks)


async def test_periodic_task_runs_on_exact_virtual_ticks():
    clock = ReplayClock(T0)
    ticks: list[datetime] = []

    async def monitor() -> None:
        while True:
            ticks.append(clock.now())
            await clock.sleep(60)

    task = asyncio.create_task(monitor())
    await clock.advance(300)
    task.cancel()
    assert ticks == [T0 + timedelta(seconds=60 * i) for i in range(6)]


async def test_two_periodic_tasks_interleave_deterministically():
    async def run() -> list[str]:
        clock = ReplayClock(T0)
        log: list[str] = []

        async def every(name: str, period: float) -> None:
            while True:
                await clock.sleep(period)
                log.append(f"{name}@{(clock.now() - T0).total_seconds():.0f}")

        tasks = [asyncio.create_task(every("md", 60)), asyncio.create_task(every("mon", 90))]
        await clock.advance(360)
        for t in tasks:
            t.cancel()
        return log

    first, second = await run(), await run()
    assert first == second
    # Equal wake times are FIFO by registration: at 180 s "mon" (slept at 90 s) precedes "md"
    # (slept at 120 s); likewise at 360 s.
    assert first == ["md@60", "mon@90", "md@120", "mon@180", "md@180", "md@240", "mon@270",
                     "md@300", "mon@360", "md@360"]  # fmt: skip


async def test_cancelled_sleepers_are_skipped():
    clock = ReplayClock(T0)
    task = asyncio.create_task(clock.sleep(30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await clock.advance(60)  # must not raise on the cancelled future
    assert clock.pending_sleepers == 0


async def test_sleep_until_the_past_returns_immediately():
    clock = ReplayClock(T0)
    await asyncio.wait_for(clock.sleep_until(T0 - timedelta(minutes=1)), timeout=1)
    await asyncio.wait_for(clock.sleep(0), timeout=1)
