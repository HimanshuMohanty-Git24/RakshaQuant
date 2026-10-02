"""
Corporate-announcement ingestion (plan M7.6).

Source: NSE's public "Latest Announcements" RSS feed (verified 2026-10-02:
``nsearchives.nseindia.com/content/RSS/Online_announcements.xml``, offered on nseindia.com's
RSS page, ``<ttl>5</ttl>``, the latest ~165 items, newest first). Each item's ``title`` is the
company name, ``description`` is ``"<text> |SUBJECT: <category>"`` and ``pubDate`` is IST
without a zone (``02-Oct-2026 00:55:31``). Only the RSS document is read - no pages are scraped.

* Polled every 5 min during the session plus once pre-open (backfill), never faster than the
  feed's own ``ttl``; conditional GET (``ETag`` / ``Last-Modified``) so an unchanged feed costs a
  304. NSE's website terms restrict automated collection: see ``docs/plan/PROGRESS.md`` OQ-2.
* Filtered to the universe by normalised company name (``Ltd.`` = ``Limited``, ``&`` = ``and``).
* Deduplicated by ``(instrument, published_at, title hash)`` - the ``announcement_id``.
* Stored as ``AnnouncementReceived`` with the exchange's ``published_at`` and our
  ``received_at`` (point in time).
* Outages are tolerated: when a fetch fails, or the new window no longer overlaps the last one
  (more announcements than the feed holds), an ``AnnouncementCoverageGap`` marks the span that
  may be missing, so downstream gates can treat it as unknown rather than "no news".
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx2

from src.domain.clock import Clock
from src.domain.events import Alert, AnnouncementCoverageGap, AnnouncementReceived
from src.domain.sink import EventSink
from src.domain.types import Instrument
from src.utils.market_time import IST

logger = logging.getLogger(__name__)

NSE_RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
SOURCE = "nse_rss"
USER_AGENT = "RakshaQuant/2 (paper-trading research; RSS reader)"
_SUFFIXES = ("limited", "ltd", "the")
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalise_name(name: str) -> str:
    """``"Bajaj Finance Ltd."`` and ``"BAJAJ FINANCE LIMITED"`` → ``"bajaj finance"``."""
    text = _PUNCT.sub(" ", name.lower().replace("&", " and "))
    words = [w for w in text.split() if w not in _SUFFIXES]
    return " ".join(words)


class CompanyMatcher:
    """Universe instruments by normalised company name (their ``name``, else the symbol)."""

    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._by_name: dict[str, Instrument] = {}
        for instrument in instruments:
            for name in (instrument.name, instrument.symbol):
                if name:
                    self._by_name.setdefault(normalise_name(name), instrument)

    def match(self, company: str) -> Instrument | None:
        return self._by_name.get(normalise_name(company))


@dataclass(frozen=True)
class FeedItem:
    company: str
    published_at: datetime  # aware
    text: str
    subject: str
    url: str | None


@dataclass(frozen=True)
class FeedPage:
    items: tuple[FeedItem, ...]
    ttl_s: float | None = None

    @property
    def newest(self) -> datetime | None:
        return max((i.published_at for i in self.items), default=None)

    @property
    def oldest(self) -> datetime | None:
        return min((i.published_at for i in self.items), default=None)


def parse_feed(body: bytes) -> FeedPage:
    """Every item of the feed (malformed items are skipped, never invented)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"announcement feed is not XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise ValueError("announcement feed has no <channel>")
    items: list[FeedItem] = []
    for node in channel.findall("item"):
        company = (node.findtext("title") or "").strip()
        published = _parse_ts(node.findtext("pubDate") or "")
        description = (node.findtext("description") or "").strip()
        if not company or published is None or not description:
            continue
        text, _, subject = description.partition("|SUBJECT:")
        url = (node.findtext("link") or "").strip() or None
        items.append(FeedItem(company, published, " ".join(text.split()), subject.strip(), url))
    ttl = channel.findtext("ttl")
    try:
        ttl_s = float(ttl) * 60 if ttl else None
    except ValueError:
        ttl_s = None
    return FeedPage(tuple(items), ttl_s)


def _parse_ts(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            ts = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=IST)
    return None


def announcement_id(instrument_key: str, published_at: datetime, title: str) -> str:
    digest = hashlib.sha256(
        f"{instrument_key}|{published_at.isoformat()}|{' '.join(title.split()).lower()}".encode()
    )
    return digest.hexdigest()[:24]


@dataclass
class PollStats:
    ok: bool
    fetched: int = 0
    new: list[AnnouncementReceived] = field(default_factory=list)
    not_modified: bool = False
    error: str | None = None


class AnnouncementIngestor:
    def __init__(
        self,
        *,
        instruments: Iterable[Instrument],
        clock: Clock,
        sink: EventSink,
        url: str = NSE_RSS_URL,
        min_interval_s: float = 300.0,
        timeout_s: float = 20.0,
        client: httpx2.AsyncClient | None = None,
        seen: Iterable[str] = (),
        last_newest: datetime | None = None,
    ) -> None:
        self._matcher = CompanyMatcher(instruments)
        self._clock = clock
        self._sink = sink
        self._url = url
        self._min_interval = timedelta(seconds=min_interval_s)
        self._timeout = timeout_s
        self._client = client or httpx2.AsyncClient(timeout=timeout_s, follow_redirects=True)
        self._seen = set(seen)
        self._validators: dict[str, str] = {}
        self._last_newest = last_newest  # newest item of the last good fetch
        self._last_fetch: datetime | None = None
        self._failing_since: datetime | None = None
        self.interval_s = min_interval_s

    async def poll(self, *, force: bool = False) -> PollStats:
        now = self._clock.now()
        if (
            not force
            and self._last_fetch is not None
            and now - self._last_fetch < self._min_interval
        ):
            return PollStats(ok=True, not_modified=True)  # never faster than the feed's ttl
        self._last_fetch = now
        try:
            response = await self._client.get(
                self._url, headers={"user-agent": USER_AGENT, **self._validators},
                timeout=self._timeout,
            )  # fmt: skip
        except httpx2.HTTPError as exc:
            return self._failed(now, f"{type(exc).__name__}")
        if response.status_code == 304:
            self._recovered()
            return PollStats(ok=True, not_modified=True)
        if response.status_code != 200:
            return self._failed(now, f"HTTP {response.status_code}")
        try:
            page = parse_feed(response.content)
        except ValueError as exc:
            return self._failed(now, str(exc))
        self._validators = {
            k: v for k, v in (("if-none-match", response.headers.get("etag")),
                              ("if-modified-since", response.headers.get("last-modified"))) if v
        }  # fmt: skip
        if page.ttl_s and page.ttl_s > self._min_interval.total_seconds():
            self._min_interval = timedelta(seconds=page.ttl_s)  # the feed asks for this pace
        self.interval_s = self._min_interval.total_seconds()
        self._check_coverage(page, now)
        self._recovered()
        stats = PollStats(ok=True, fetched=len(page.items))
        for item in sorted(page.items, key=lambda i: i.published_at):
            event = self._accept(item, now)
            if event is not None:
                stats.new.append(event)
        if page.newest is not None:
            self._last_newest = max(page.newest, self._last_newest or page.newest)
        return stats

    def _accept(self, item: FeedItem, now: datetime) -> AnnouncementReceived | None:
        instrument = self._matcher.match(item.company)
        if instrument is None:
            return None
        aid = announcement_id(instrument.key, item.published_at, item.text)
        if aid in self._seen:
            return None
        self._seen.add(aid)
        event = AnnouncementReceived(
            announcement_id=aid, instrument_key=instrument.key, company=item.company,
            published_at=item.published_at, received_at=now, title=item.text[:2000],
            subject=item.subject[:200], url=item.url, source=SOURCE,
        )  # fmt: skip
        self._sink.emit(event, source="announcements")
        return event

    def _check_coverage(self, page: FeedPage, now: datetime) -> None:
        oldest = page.oldest
        if self._last_newest is None or oldest is None:
            return  # first fetch: nothing to overlap with
        if oldest > self._last_newest:  # the window moved past what we last saw
            self._gap(self._last_newest, oldest, "feed_window_exceeded")

    def _failed(self, now: datetime, error: str) -> PollStats:
        logger.warning("announcement feed fetch failed: %s", error)
        if self._failing_since is None:
            self._failing_since = now
            self._sink.emit(Alert(level="WARNING", key="announcements_feed_down",
                                  message=f"announcement feed unavailable: {error}"),
                            source="announcements")  # fmt: skip
        return PollStats(ok=False, error=error)

    def _recovered(self) -> None:
        self._failing_since = None

    def _gap(self, start: datetime, end: datetime, reason: str) -> None:
        self._sink.emit(AnnouncementCoverageGap(source=SOURCE, gap_from=start, gap_to=end,
                                                reason=reason), source="announcements")  # fmt: skip
        logger.warning("announcement coverage gap %s → %s (%s)", start, end, reason)

    async def close(self) -> None:
        await self._client.aclose()


def watermark(events: Iterable[AnnouncementReceived]) -> tuple[set[str], datetime | None]:
    """The dedupe set and the newest ``published_at`` of stored announcements (for a restart)."""
    seen: set[str] = set()
    newest: datetime | None = None
    for event in events:
        seen.add(event.announcement_id)
        newest = event.published_at if newest is None else max(newest, event.published_at)
    return seen, newest
