"""Plan M1.2: the event envelope, its lineage keys, and the payload catalogue."""

import json
from datetime import UTC, datetime

import pytest

import src.domain.events as ev
import src.domain.types as dt
from src.domain.base import EventPayload
from tests.factories import INFY, T0, order, quote, sample_payloads


def _concrete_payload_classes() -> set[type[EventPayload]]:
    found: set[type[EventPayload]] = set()
    stack = list(EventPayload.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if "event_type" in cls.__dict__ and cls.__module__ in (ev.__name__, dt.__name__):
            found.add(cls)
    return found


def test_catalogue_registers_every_payload_class():
    assert set(ev.EVENT_TYPES.values()) == _concrete_payload_classes()
    for name, cls in ev.EVENT_TYPES.items():
        assert cls.event_type == name


def test_samples_cover_the_whole_catalogue():
    assert {p.event_type for p in sample_payloads()} == set(ev.EVENT_TYPES)


@pytest.mark.parametrize("payload", sample_payloads(), ids=lambda p: p.event_type)
def test_every_payload_round_trips_through_storage_json(payload):
    stored = ev.payload_json(payload)
    assert ev.load_payload(payload.event_type, payload.schema_version, stored) == payload
    assert (
        ev.load_payload(payload.event_type, payload.schema_version, json.loads(stored)) == payload
    )


def test_payload_json_is_canonical():
    payload = ev.OrderSubmitted(order=order())
    text = ev.payload_json(payload)
    assert text == ev.payload_json(ev.load_payload("OrderSubmitted", 1, text))
    assert ", " not in text and ": " not in text
    data = json.loads(text)
    assert list(data) == sorted(data)
    assert data["order"]["intent"]["decision_price"] == "1512.40"  # exact decimal as text


def test_make_event_stamps_utc_and_ist_date():
    late_evening_utc = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)  # 01:30 IST on 2 Oct
    e = ev.make_event(ev.Heartbeat(pid=1, uptime_s=0), ts=late_evening_utc, source="test")
    assert e.ts_utc == late_evening_utc
    assert str(e.ist_date) == "2026-10-02"
    assert e.type == "Heartbeat" and e.schema_version == 1 and e.seq is None


def test_make_event_rejects_naive_timestamps_and_missing_source():
    with pytest.raises(ValueError, match="timezone-aware"):
        ev.make_event(ev.Heartbeat(pid=1, uptime_s=0), ts=datetime(2026, 10, 5), source="x")
    with pytest.raises(ValueError, match="source"):
        ev.make_event(ev.Heartbeat(pid=1, uptime_s=0), ts=T0, source="")


def test_lineage_keys_come_from_the_payload():
    e = ev.make_event(ev.OrderSubmitted(order=order(book_id="B")), ts=T0, source="oms")
    assert (e.decision_id, e.book_id, e.symbol) == ("d-0001", "B", "INFY")
    e = ev.make_event(ev.QuoteReceived(quote=quote()), ts=T0, source="md")
    assert e.symbol == "INFY" and e.decision_id is None


def test_explicit_lineage_must_not_contradict_the_payload():
    payload = ev.OrderSubmitted(order=order(book_id="B"))
    ev.make_event(payload, ts=T0, source="oms", book_id="B", cycle_id="cy-1")
    with pytest.raises(ValueError, match="contradicts"):
        ev.make_event(payload, ts=T0, source="oms", book_id="A")


def test_unknown_types_and_versions_are_refused():
    with pytest.raises(ev.UnknownEventTypeError):
        ev.load_payload("NoSuchEvent", 1, "{}")
    with pytest.raises(ev.SchemaVersionError):
        ev.load_payload("Heartbeat", 2, '{"pid": 1, "uptime_s": 0}')


def test_to_dict_is_json_serialisable():
    e = ev.make_event(ev.OrderSubmitted(order=order()), ts=T0, source="oms").with_seq(7)
    data = json.loads(json.dumps(e.to_dict()))
    assert data["seq"] == 7 and data["symbol"] == INFY.symbol and data["book_id"] == "A"
