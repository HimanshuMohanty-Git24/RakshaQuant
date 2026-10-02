"""
The WebSocket event stream (plan M9.4; audit §N.3).

Every message is an envelope ``{v, seq, type, topic, ts, decision_id, book_id, data}``. A client
sends ``{"subscribe": [topics], "since_seq": n}``; the server **replays** the stored events after
``n`` for those topics, then **tails** new ones, with nothing lost or repeated in between:

* the hub's tailer polls the store (on the web's own read connection) and appends each new event
  to the queue of every subscriber of its topic; a subscription snapshots the tailer's position
  (``replay_to``) synchronously, so the replay covers ``(since_seq, replay_to]`` and the queue
  holds exactly what came after;
* a queue that overflows is dropped for a ``resync`` message carrying the last ``seq`` sent; the
  client re-subscribes from there;
* ``summary``, ``quotes`` and ``console`` are **latest-wins slots**, not queued: a slow client
  gets the newest value, never a backlog (quotes conflated to at most 1 Hz);
* an idle connection gets a ``heartbeat`` every 15 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.domain.events import EVENT_TYPES, Event, payload_json

if TYPE_CHECKING:
    from src.store.event_store import EventStore
    from src.web.run_manager import RunManager

logger = logging.getLogger(__name__)

VERSION = 1
_TOPIC_OF: dict[str, str] = {}
for _topic, _types in {
    "orders": ("OrderSubmitted", "OrderAcked", "OrderRejected", "OrderUnknown", "OrderCancelled",
               "OrderExpired", "FillReceived"),
    "positions": ("PositionChanged", "TradeClosed", "MarkToMarket"),
    "decisions": ("SignalGenerated", "AdvisorRequested", "AdvisorVerdict", "AdvisorFallback",
                  "OrderIntentProposed", "SignalDisposition", "RiskDecision", "ShadowTradeOpened",
                  "ShadowTradeClosed", "ShadowAlphaSettled"),
    "risk": ("KillSwitchChanged", "LimitBreached", "DailyRiskStateRolled", "ReconciliationResult",
             "ControlCommand"),
    "ai": ("LLMCall", "DecisionModelCall", "BudgetThresholdCrossed", "TradeReview"),
    "market": ("QuoteReceived", "QuoteRejected", "BarClosed", "DataSourceChanged", "FeedStale",
               "FeedRecovered", "AnnouncementReceived", "AnnouncementCoverageGap",
               "RegimeComputed", "TypedEvent"),
    "system": ("SessionStateChanged", "HolidaySkipped", "Heartbeat", "LoopLag", "Alert",
               "ProcessStarted", "ProcessStopped"),
}.items():  # fmt: skip
    for _type in _types:
        _TOPIC_OF[_type] = _topic

EVENT_TOPICS = frozenset(_TOPIC_OF.values())
SLOT_TOPICS = frozenset({"summary", "quotes", "console"})
TOPICS = EVENT_TOPICS | SLOT_TOPICS
assert set(_TOPIC_OF) == set(EVENT_TYPES), "every event type needs a topic"


def topic_of(event_type: str) -> str:
    return _TOPIC_OF.get(event_type, "system")


def envelope(event: Event) -> dict[str, Any]:
    return {"v": VERSION, "seq": event.seq, "type": event.type, "topic": topic_of(event.type),
            "ts": event.ts_utc.isoformat(), "decision_id": event.decision_id,
            "book_id": event.book_id, "data": json.loads(payload_json(event.payload))}  # fmt: skip


def notice(kind: str, data: Mapping[str, Any], *, topic: str = "system") -> dict[str, Any]:
    """A message that is not a stored event (heartbeat, resync, slots, run notices)."""
    return {"v": VERSION, "seq": None, "type": kind, "topic": topic,
            "ts": datetime.now(UTC).isoformat(), "decision_id": None, "book_id": None,
            "data": dict(data)}  # fmt: skip


@dataclass(eq=False)  # identity: subscribers live in a set
class Subscriber:
    queue_max: int
    topics: frozenset[str] = frozenset()
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    paused: bool = True  # until the first subscribe, and after an overflow
    replay: tuple[int, int] | None = None  # (since_seq, replay_to) still to send
    ack: dict[str, Any] | None = None  # "subscribed", sent before the replay
    last_sent: int = 0

    def push(self, message: dict[str, Any]) -> None:
        if self.paused:
            return
        if len(self.queue) >= self.queue_max:  # a slow client: drop the backlog, ask to resync
            self.queue.clear()
            self.paused = True
            self.replay = None
            self.queue.append(notice("resync", {"since_seq": self.last_sent,
                                                "reason": "overflow"}))  # fmt: skip
        else:
            self.queue.append(message)
        self.wake.set()

    def put_slot(self, topic: str, message: dict[str, Any]) -> None:
        if topic in self.topics and not self.paused:
            self.slots[topic] = message
            self.wake.set()


class Hub:
    """Tails the store and fans events out; owned by the :class:`RunManager`."""

    def __init__(
        self,
        manager: RunManager,
        *,
        poll_s: float = 0.25,
        batch: int = 500,
        queue_max: int = 2000,
        heartbeat_s: float = 15.0,
        slot_interval_s: float = 1.0,
    ) -> None:
        self.manager = manager
        self.poll_s = poll_s
        self.batch = batch
        self.queue_max = queue_max
        self.heartbeat_s = heartbeat_s
        self.slot_interval_s = slot_interval_s
        self.subscribers: set[Subscriber] = set()
        self.tail_seq: int | None = None
        self._store: EventStore | None = None
        self._fingerprint: tuple[str, str] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._last_quotes: dict[str, Any] | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    def ensure_started(self) -> None:
        if not self._tasks or all(t.done() for t in self._tasks):
            self._tasks = [asyncio.create_task(self._tail(), name="web:tail"),
                           asyncio.create_task(self._slots(), name="web:slots")]  # fmt: skip

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # -- subscriptions ---------------------------------------------------------------------------

    def connect(self) -> Subscriber:
        self.ensure_started()
        sub = Subscriber(queue_max=self.queue_max)
        self.subscribers.add(sub)
        return sub

    def disconnect(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def subscribe(self, sub: Subscriber, topics: frozenset[str], since_seq: int) -> None:
        """(Re)subscribe. Synchronous on purpose: the tailer cannot run in between, so the
        replay window ends exactly where the live queue begins."""
        head = self._head()
        sub.topics = topics
        sub.queue.clear()
        sub.slots.clear()
        sub.paused = False
        sub.last_sent = min(since_seq, head)
        sub.replay = (sub.last_sent, head) if topics & EVENT_TOPICS and since_seq < head else None
        sub.ack = notice("subscribed", {"topics": sorted(topics), "since_seq": sub.last_sent,
                                        "replay_to": head})  # fmt: skip
        sub.wake.set()

    def _head(self) -> int:
        if self.tail_seq is None:  # first use: start tailing from the store's end
            store = self.manager.queries().store
            self.tail_seq = store.last_seq()
            self._store, self._fingerprint = store, _fingerprint(store)
        return self.tail_seq

    def publish(self, topic: str, data: Mapping[str, Any]) -> None:
        """Latest-wins slot (``console``, ``summary``, ``quotes``)."""
        message = notice(topic, data, topic=topic)
        for sub in self.subscribers:
            sub.put_slot(topic, message)

    def announce(self, kind: str, data: Mapping[str, Any]) -> None:
        """A run notice (``stopped``, ``error``) to every subscriber of the system topic."""
        message = notice(kind, data)
        for sub in self.subscribers:
            if "system" in sub.topics:
                sub.push(message)

    # -- sending ---------------------------------------------------------------------------------

    async def send_loop(self, sub: Subscriber, send: Any) -> None:
        """Deliver one subscriber's messages in order: replay, queue, then slots."""
        while True:
            if sub.ack is None and sub.replay is None and not sub.queue and not sub.slots:
                try:
                    await asyncio.wait_for(sub.wake.wait(), timeout=self.heartbeat_s)
                except TimeoutError:
                    await send(notice("heartbeat", {"last_seq": self.tail_seq or 0}))
                    continue
            sub.wake.clear()
            if sub.ack is not None:
                ack, sub.ack = sub.ack, None
                await send(ack)
            if sub.replay is not None:
                await self._send_replay(sub, send)
            while sub.queue:
                message = sub.queue.popleft()
                await send(message)
                if message.get("seq") is not None:
                    sub.last_sent = int(message["seq"])
            for topic in list(sub.slots):
                await send(sub.slots.pop(topic))

    async def _send_replay(self, sub: Subscriber, send: Any) -> None:
        assert sub.replay is not None
        cursor, until = sub.replay
        store = self.manager.queries().store
        topics = sub.topics
        while cursor < until and sub.replay is not None:
            events = await asyncio.to_thread(store.read, since_seq=cursor,
                                             limit=min(self.batch, until - cursor))  # fmt: skip
            if not events:
                break
            for event in events:
                seq = event.seq or 0
                if seq > until:
                    break
                if topic_of(event.type) in topics:
                    await send(envelope(event))
                sub.last_sent = cursor = seq
            if sub.replay is None:  # an overflow during the replay: the client resyncs
                return
            sub.replay = (cursor, until)
        sub.replay = None
        sub.last_sent = max(sub.last_sent, until)

    # -- the tailer and the slots ------------------------------------------------------------------

    async def _tail(self) -> None:
        while True:
            try:
                await self._tail_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # the stream never takes the server down
                logger.exception("event stream tail failed")
            await asyncio.sleep(self.poll_s)

    async def _tail_once(self) -> None:
        store = self.manager.queries().store
        if store is not self._store:  # a new connection: the same database, or a new one?
            previous = self._fingerprint
            self._store, self._fingerprint = store, _fingerprint(store)
            if previous is not None and self._fingerprint != previous:
                self.tail_seq = store.last_seq()  # a demo restart: everyone starts over
                for sub in self.subscribers:
                    sub.queue.clear()
                    sub.replay = None
                    sub.queue.append(notice("resync", {"since_seq": 0, "reason": "new_store"}))
                    sub.paused = True
                    sub.wake.set()
                return
        head = self._head()
        events = await asyncio.to_thread(store.read, since_seq=head, limit=self.batch)
        if not events or store is not self._store:
            return
        self.tail_seq = events[-1].seq or head  # synchronous from here: fan out
        for event in events:
            topic = topic_of(event.type)
            message: dict[str, Any] | None = None
            for sub in self.subscribers:
                if topic in sub.topics:
                    message = message or envelope(event)
                    sub.push(message)

    async def _slots(self) -> None:
        while True:
            await asyncio.sleep(self.slot_interval_s)
            try:
                await self._publish_slots()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event stream slots failed")

    async def _publish_slots(self) -> None:
        wanted = set().union(*(s.topics for s in self.subscribers)) if self.subscribers else set()
        manager = self.manager
        if "summary" in wanted:
            live, running = manager.live_view(), manager.is_running
            queries = manager.queries()
            summary = await asyncio.to_thread(queries.summary, live, running=running)
            self.publish("summary", summary.model_dump(mode="json"))
        engine = manager.engine
        if "quotes" in wanted and engine is not None:
            quotes = {
                key: {"symbol": key.rsplit(":", 1)[-1], "ltp": q.ltp, "prev_close": q.prev_close,
                      "change_pct": round((q.ltp / q.prev_close - 1) * 100, 3)
                      if q.prev_close else None,
                      "exchange_ts": q.exchange_ts.isoformat(), "source": q.source.value}
                for key, q in sorted(engine.market.quotes().items())
            }  # fmt: skip
            if quotes != self._last_quotes:  # conflated: at most one message per interval
                self._last_quotes = quotes
                self.publish("quotes", {"quotes": quotes})


async def serve(hub: Hub, sub: Subscriber, websocket: Any) -> None:
    """Run one connection: a receiver for subscribe messages and the sender loop."""

    async def send(message: dict[str, Any]) -> None:
        await websocket.send_json(message)

    sender = asyncio.create_task(hub.send_loop(sub, send))
    try:
        while True:
            raw = await websocket.receive_text()
            request = _parse(raw)
            if request is None:
                await send(notice("error", {"message": "bad request"}))
                continue
            topics, since = request
            hub.subscribe(sub, topics, since)
            if sender.done():
                return
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender


def _fingerprint(store: EventStore) -> tuple[str, str] | None:
    """Identifies a database by its first event (a demo restart recreates the file)."""
    first = store.read(limit=1)
    return (first[0].ts_utc.isoformat(), first[0].type) if first else None


def _parse(raw: str) -> tuple[frozenset[str], int] | None:
    if len(raw) > 4096:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict) or set(message) - {"subscribe", "since_seq"}:
        return None
    topics, since = message.get("subscribe"), message.get("since_seq", 0)
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        return None
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        return None
    wanted = frozenset(topics)
    if "events" in wanted:
        wanted = (wanted - {"events"}) | EVENT_TOPICS
    if not wanted or not wanted <= TOPICS:
        return None
    return wanted, since
