"""Plan M1.3: time-ordered ids, deterministic intent ids and broker tags."""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from src.domain import ids

KEY = "NSE:EQ:INFY"
BAR = date(2026, 10, 2)


def test_new_id_shape():
    assert re.fullmatch(r"[0-9a-f]{24}", ids.new_id())


def test_new_ids_are_strictly_increasing_and_unique():
    batch = [ids.new_id() for _ in range(5000)]
    assert batch == sorted(batch)
    assert len(set(batch)) == len(batch)
    assert len({i[:16] for i in batch}) == len(batch)  # the time part alone never repeats


def test_new_ids_are_unique_across_threads():
    with ThreadPoolExecutor(max_workers=8) as pool:
        batch = list(pool.map(lambda _: ids.new_id(), range(4000)))
    assert len(set(batch)) == len(batch)


def test_intent_id_is_deterministic_and_readable():
    a = ids.intent_id(
        book_id="A", strategy="momentum", instrument_key=KEY, signal_bar_date=BAR, leg="entry"
    )
    assert a == "A:momentum:NSE:EQ:INFY:2026-10-02:entry"
    again = ids.intent_id(
        book_id="A", strategy="momentum", instrument_key=KEY, signal_bar_date=BAR, leg="entry"
    )
    assert a == again


def test_books_and_legs_get_distinct_intents_and_tags():
    def make(book: str, leg: str = "entry") -> str:
        return ids.intent_id(
            book_id=book, strategy="momentum", instrument_key=KEY, signal_bar_date=BAR, leg=leg
        )

    intents = {make("A"), make("B"), make("C"), make("A", "stop"), make("A", "exit-1")}
    assert len(intents) == 5
    assert len({ids.client_order_id(i) for i in intents}) == 5


@pytest.mark.parametrize(
    "field", [{"book_id": "A:B"}, {"strategy": ""}, {"leg": "x:y"}, {"instrument_key": ""}]
)
def test_intent_id_rejects_ambiguous_parts(field):
    args = {
        "book_id": "A",
        "strategy": "momentum",
        "instrument_key": KEY,
        "signal_bar_date": BAR,
        "leg": "entry",
    }
    args.update(field)
    with pytest.raises(ValueError):
        ids.intent_id(**args)


def test_client_order_id_is_sha256_prefix():
    intent = "A:momentum:NSE:EQ:INFY:2026-10-02:entry"
    tag = ids.client_order_id(intent)
    assert tag == hashlib.sha256(intent.encode()).hexdigest()[:16]
    assert re.fullmatch(r"[0-9a-f]{16}", tag)
    with pytest.raises(ValueError):
        ids.client_order_id("")
