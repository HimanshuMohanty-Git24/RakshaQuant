"""Plan M7.6: NSE announcement ingestion - parse, universe filter, dedupe, point in time,
conditional GET, polling never faster than the feed's ttl, coverage gaps."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

import httpx2

from src.domain.clock import ReplayClock
from src.domain.events import AnnouncementReceived
from src.domain.sink import RecordingSink
from src.domain.types import Instrument
from src.marketdata.announcements import (
    NSE_RSS_URL,
    AnnouncementIngestor,
    normalise_name,
    parse_feed,
    watermark,
)
from src.utils.market_time import IST

FEED = (Path(__file__).parent / "fixtures" / "nse_announcements.xml").read_bytes()
UNIVERSE = [
    Instrument.nse_equity("INFY", name="Infosys Ltd."),
    Instrument.nse_equity("BAJFINANCE", name="Bajaj Finance Ltd."),
    Instrument.nse_equity("M&M", name="Mahindra & Mahindra Ltd."),
]
T0 = datetime.combine(datetime(2026, 10, 5).date(), time(9, 50), IST)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(T0.date(), time(hh, mm, ss), IST)


class Server:
    """Answers with the current feed body; honours If-None-Match; can fail on demand."""

    def __init__(self, body: bytes = FEED) -> None:
        self.body, self.status, self.requests = body, 200, []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx2.Response(self.status)
        etag = f'"{hash(self.body) & 0xFFFF:x}"'
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        return httpx2.Response(200, content=self.body, headers={"etag": etag,
                               "content-type": "application/xml"})  # fmt: skip


def ingestor(server: Server, clock: ReplayClock, sink: RecordingSink, **kw) -> AnnouncementIngestor:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
    return AnnouncementIngestor(instruments=UNIVERSE, clock=clock, sink=sink, client=client, **kw)


def test_parse_keeps_well_formed_items_in_ist():
    page = parse_feed(FEED)
    assert len(page.items) == 5 and page.ttl_s == 300.0  # two malformed items skipped
    infy = page.items[1]
    assert infy.company == "Infosys Limited" and infy.published_at == at(9, 30, 1)
    assert infy.subject == "Board Meeting Intimation" and infy.url.endswith("_BM.pdf")
    assert infy.text.startswith("Infosys Limited has informed") and "|SUBJECT" not in infy.text
    assert page.newest == at(9, 41) and page.oldest == at(9, 1)


def test_company_names_normalise():
    assert normalise_name("Bajaj Finance Ltd.") == normalise_name("BAJAJ FINANCE LIMITED")
    assert normalise_name("Mahindra & Mahindra Ltd.") == "mahindra and mahindra"
    assert normalise_name("Bajaj Electricals Limited") != normalise_name("Bajaj Finance Ltd.")


async def test_universe_announcements_are_stored_once_point_in_time():
    clock, server = ReplayClock(T0), Server()
    sink = RecordingSink(clock)
    ing = ingestor(server, clock, sink)
    stats = await ing.poll()
    assert stats.ok and stats.fetched == 5
    stored = [e.payload for e in sink.events if e.type == "AnnouncementReceived"]
    assert [(a.instrument_key, a.published_at) for a in stored] == [
        ("NSE:EQ:M&M", at(9, 1)), ("NSE:EQ:BAJFINANCE", at(9, 12, 49)), ("NSE:EQ:INFY", at(9, 30, 1)),
    ]  # oldest first; the ETF NAV and Bajaj Electricals are not in the universe  # fmt: skip
    assert all(a.received_at == T0 and a.source == "nse_rss" for a in stored)
    assert (
        server.requests[0].url == NSE_RSS_URL
        and "RakshaQuant" in server.requests[0].headers["user-agent"]
    )

    await clock.advance(300)
    again = await ing.poll()
    assert again.not_modified and server.requests[-1].headers["if-none-match"]  # conditional GET
    assert len([e for e in sink.events if e.type == "AnnouncementReceived"]) == 3


async def test_polling_never_runs_faster_than_the_feed_ttl():
    clock, server = ReplayClock(T0), Server()
    ing = ingestor(server, clock, RecordingSink(clock), min_interval_s=60)
    await ing.poll()
    assert ing.interval_s == 300.0  # raised to the feed's <ttl>5</ttl>
    await clock.advance(120)
    assert (await ing.poll()).not_modified and len(server.requests) == 1  # skipped locally


async def test_a_restart_does_not_store_duplicates(tmp_path):
    clock, server = ReplayClock(T0), Server()
    first_sink = RecordingSink(clock)
    await ingestor(server, clock, first_sink).poll()
    stored = [e.payload for e in first_sink.events if isinstance(e.payload, AnnouncementReceived)]
    seen, newest = watermark(stored)
    sink = RecordingSink(clock)
    reborn = ingestor(Server(), clock, sink, seen=seen, last_newest=newest)
    assert (await reborn.poll()).new == [] and sink.events == []


async def test_a_moved_window_is_a_coverage_gap_and_an_outage_alerts_once():
    clock, server = ReplayClock(T0), Server()
    sink = RecordingSink(clock)
    ing = ingestor(server, clock, sink, last_newest=at(8, 0))  # last saw 08:00; feed starts 09:01
    await ing.poll()
    (gap,) = [e.payload for e in sink.events if e.type == "AnnouncementCoverageGap"]
    assert (gap.gap_from, gap.gap_to, gap.reason) == (at(8, 0), at(9, 1), "feed_window_exceeded")

    server.status = 503
    for _ in range(3):
        await clock.advance(300)
        assert not (await ing.poll()).ok
    assert [a.payload.key for a in sink.events if a.type == "Alert"] == ["announcements_feed_down"]

    server.status, server.body = 200, FEED.replace(b"05-Oct-2026 09:01:00", b"05-Oct-2026 09:45:00")
    await clock.advance(300)
    assert (await ing.poll()).ok  # overlaps what was seen (newest 09:41 >= oldest 09:05): no gap
    assert len([e for e in sink.events if e.type == "AnnouncementCoverageGap"]) == 1


async def test_garbage_is_a_failed_fetch_not_a_crash():
    clock, server = ReplayClock(T0), Server(b"<html>maintenance</html>")
    stats = await ingestor(server, clock, RecordingSink(clock)).poll()
    assert not stats.ok and "channel" in (stats.error or "")


def test_the_feed_window_moves_past_old_items_only_when_overlap_is_lost():
    page = parse_feed(FEED)
    assert page.oldest is not None and page.oldest + timedelta(minutes=40) == at(9, 41)
