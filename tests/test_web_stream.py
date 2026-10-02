"""Plan M9.4: the WebSocket event stream - replay then tail, resume, slots, heartbeats."""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.domain.clock import ReplayClock
from src.domain.events import EVENT_TYPES, Alert
from src.store.event_store import EventStore
from src.store.sink import StoreSink
from src.web.run_manager import RunManager
from src.web.server import create_app
from src.web.stream import EVENT_TOPICS, Hub, Subscriber, _parse, notice, topic_of
from tests.factories import sample_payloads
from tests.web_helpers import AUTH, BASE, PROTOCOLS, SECURITY, WS_URL, authed

T0 = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)


def emit_alerts(path: Path, n: int, start: int = 0) -> None:
    with EventStore(path) as store:
        sink = StoreSink(store, ReplayClock(T0), "test")
        for i in range(start, start + n):
            sink.emit(Alert(level="INFO", key=f"k{i}", message=f"alert {i}"))


@pytest.fixture
def manager(settings, tmp_path):
    m = RunManager(settings, store_path=tmp_path / "rq.db")
    m.hub.poll_s = 0.02
    yield m
    m.close_reader()


def receive_until(ws: Any, count: int, kind: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while len(out) < count:
        message = ws.receive_json()
        if kind is None or message["type"] == kind:
            out.append(message)
    return out


def test_every_event_type_has_a_topic():
    assert {topic_of(t) for t in EVENT_TYPES} == EVENT_TOPICS
    assert topic_of("OrderSubmitted") == "orders" and topic_of("ControlCommand") == "risk"


def test_replay_then_tail_without_gaps(manager, tmp_path):
    emit_alerts(tmp_path / "rq.db", 5)
    client = authed(create_app(manager=manager, security=SECURITY))
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as ws:
        ws.send_json({"subscribe": ["system"], "since_seq": 2})
        ack = ws.receive_json()
        assert ack["type"] == "subscribed" and ack["data"]["replay_to"] == 5
        replayed = receive_until(ws, 3)
        assert [m["seq"] for m in replayed] == [3, 4, 5]
        assert replayed[0]["data"]["key"] == "k2" and replayed[0]["topic"] == "system"
        assert set(replayed[0]) == {"v", "seq", "type", "topic", "ts", "decision_id", "book_id",
                                    "data"}  # fmt: skip
        emit_alerts(tmp_path / "rq.db", 3, start=5)  # written by another connection
        live = receive_until(ws, 3, "Alert")
        assert [m["seq"] for m in live] == [6, 7, 8]


def test_resuming_from_since_seq_loses_nothing(manager, tmp_path):
    db = tmp_path / "rq.db"
    emit_alerts(db, 10)
    client = authed(create_app(manager=manager, security=SECURITY))
    seen: list[int] = []
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as ws:
        ws.send_json({"subscribe": ["events"], "since_seq": 0})
        ws.receive_json()  # subscribed
        seen += [m["seq"] for m in receive_until(ws, 4)]
    emit_alerts(db, 6, start=10)  # while disconnected
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as ws:
        ws.send_json({"subscribe": ["events"], "since_seq": seen[-1]})
        ws.receive_json()
        seen += [m["seq"] for m in receive_until(ws, 12)]
        emit_alerts(db, 2, start=16)  # and live again
        seen += [m["seq"] for m in receive_until(ws, 2, "Alert")]
    assert seen == list(range(1, 19))  # every event exactly once, in order


def test_topics_filter_and_bad_requests_are_rejected(manager, tmp_path):
    with EventStore(tmp_path / "rq.db") as store:
        sink = StoreSink(store, ReplayClock(T0), "test")
        for payload in sample_payloads():
            sink.emit(payload)
    client = authed(create_app(manager=manager, security=SECURITY))
    with client.websocket_connect(WS_URL, subprotocols=PROTOCOLS) as ws:
        ws.send_json({"subscribe": ["orders"], "since_seq": 0})
        ack = ws.receive_json()
        orders = [m for m in receive_until(ws, 7)]
        assert {m["topic"] for m in orders} == {"orders"}
        assert ack["data"]["topics"] == ["orders"]
        for bad in ("not json", '{"subscribe": ["everything"]}', '{"subscribe": []}',
                    '{"subscribe": ["orders"], "since_seq": -1}',
                    '{"subscribe": ["orders"], "since_seq": true}',
                    '{"subscribe": ["orders"], "token": "x"}'):  # fmt: skip
            ws.send_text(bad)
            message = receive_until(ws, 1, "error")[0]
            assert message["data"] == {"message": "bad request"}


def test_parse():
    assert _parse('{"subscribe": ["events", "summary"]}') == (EVENT_TOPICS | {"summary"}, 0)
    assert _parse('{"subscribe": ["quotes"], "since_seq": 7}') == (frozenset({"quotes"}), 7)
    assert _parse("x" * 5000) is None


def test_an_overflowing_subscriber_is_asked_to_resync():
    sub = Subscriber(queue_max=3, topics=frozenset({"system"}), paused=False, last_sent=41)
    for i in range(3):
        sub.push({"seq": 42 + i})
    sub.push({"seq": 45})  # one too many
    assert list(sub.queue) == [notice("resync", {"since_seq": 41, "reason": "overflow"})
                               | {"ts": sub.queue[0]["ts"]}]  # fmt: skip
    assert sub.paused
    sub.push({"seq": 46})  # nothing more until the client re-subscribes
    assert len(sub.queue) == 1


def test_slots_are_latest_wins():
    sub = Subscriber(queue_max=10, topics=frozenset({"summary"}), paused=False)
    sub.put_slot("summary", {"n": 1})
    sub.put_slot("summary", {"n": 2})
    sub.put_slot("quotes", {"n": 3})  # not subscribed
    assert sub.slots == {"summary": {"n": 2}}


async def test_a_replay_carries_on_when_a_session_swaps_the_connection(manager, tmp_path):
    emit_alerts(tmp_path / "rq.db", 6)
    hub = manager.hub
    hub.batch = 2
    sub = Subscriber(queue_max=100, topics=frozenset({"system"}), paused=False)
    sub.replay = (0, 6)
    sent: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["seq"])
        if len(sent) == 2:
            manager.close_reader()  # a session starts mid-replay: the old connection is closed

    await hub._send_replay(sub, send)
    assert sent == [1, 2, 3, 4, 5, 6] and sub.replay is None


async def test_an_idle_connection_gets_heartbeats(manager):
    hub = Hub(manager, heartbeat_s=0.05)
    sub = Subscriber(queue_max=10)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    loop = asyncio.create_task(hub.send_loop(sub, send))
    await asyncio.sleep(0.2)
    loop.cancel()
    assert sent and all(m["type"] == "heartbeat" for m in sent)


async def test_quotes_are_conflated(manager):
    class Quote:
        def __init__(self, ltp: float) -> None:
            self.ltp, self.prev_close = ltp, 100.0
            self.exchange_ts, self.source = T0, type("S", (), {"value": "yfinance"})()

    class Market:
        prices = {"NSE:EQ:INFY": Quote(101.0)}

        def quotes(self) -> dict[str, Any]:
            return dict(self.prices)

    manager.engine = type("E", (), {"market": Market()})()  # type: ignore[assignment]
    hub = manager.hub
    sub = Subscriber(queue_max=10, topics=frozenset({"quotes"}), paused=False)
    hub.subscribers.add(sub)
    await hub._publish_slots()
    assert sub.slots["quotes"]["data"]["quotes"]["NSE:EQ:INFY"]["change_pct"] == 1.0
    sub.slots.clear()
    await hub._publish_slots()  # unchanged: nothing new
    assert sub.slots == {}
    Market.prices["NSE:EQ:INFY"] = Quote(102.0)
    await hub._publish_slots()
    assert sub.slots["quotes"]["data"]["quotes"]["NSE:EQ:INFY"]["ltp"] == 102.0
    manager.engine = None


async def test_health_stays_fast_while_the_engine_polls_and_the_stream_replays(manager, tmp_path):
    """Acceptance: /api/health p99 < 100 ms during a (blocking, off-loop) market-data poll,
    with REST reads and a stream tail running on the same event loop."""
    emit_alerts(tmp_path / "rq.db", 2000)
    app = create_app(manager=manager, security=SECURITY)
    stop = asyncio.Event()

    async def poll() -> None:  # what YFinanceQuoteSource does: the fetch runs in a thread
        while not stop.is_set():
            await asyncio.to_thread(time.sleep, 0.2)

    async def reader(client: httpx.AsyncClient) -> None:
        while not stop.is_set():
            await client.get("/api/decisions", params={"limit": 1000})
            await client.get("/api/system")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=AUTH) as client:
        sub = manager.hub.connect()
        manager.hub.subscribe(sub, EVENT_TOPICS, 0)
        sink: list[dict[str, Any]] = []

        async def collect(message: dict[str, Any]) -> None:
            sink.append(message)

        background = [asyncio.create_task(poll()), asyncio.create_task(reader(client)),
                      asyncio.create_task(manager.hub.send_loop(sub, collect))]  # fmt: skip
        latencies = []
        for _ in range(200):
            start = time.perf_counter()
            assert (await client.get("/api/health")).status_code == 200
            latencies.append((time.perf_counter() - start) * 1000)
            await asyncio.sleep(0.005)
        stop.set()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await manager.hub.stop()
    p99 = statistics.quantiles(latencies, n=100)[98]
    assert p99 < 100, f"p99 {p99:.1f} ms"
    assert len([m for m in sink if m["type"] == "Alert"]) == 2000  # the replay completed
