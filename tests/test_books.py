"""Plan M8.1-8.2: the experiment config and paired books A/B/C on one shared engine."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.config.errors import ConfigError
from src.config.limits import load_risk_limits
from src.decision.advisors.llm_veto import LLMVetoAdvisor
from src.decision.advisors.typed_veto import TypedVetoAdvisor
from src.domain.calendar import get_calendar
from src.domain.clock import ReplayClock
from src.domain.events import Disposition
from src.domain.sink import RecordingSink
from src.domain.types import Product, Verdict
from src.engine.runner import Engine, EngineConfig, build_engine
from src.evaluation.books import AbstainAdvisor, build_advisors, engine_config
from src.evaluation.experiment import DEFAULT_EXPERIMENT_PATH, ExperimentConfig, load_experiment
from src.features.technical import Features
from src.llm.registry import ModelSpec, RoleConfig
from src.marketdata.replay import TapeHistorySource, TapeQuoteSource
from src.store.event_store import EventStore
from src.store.tape import TapeWriter, read_bars, read_quotes
from src.strategies.policy import Proposal
from tests.test_engine_replay import DAY, INFY, TCS, at, history, ramp, session_quotes

CAL = get_calendar()


# --- 8.1 the experiment --------------------------------------------------------------------------


def test_the_month1_experiment_loads():
    e = load_experiment()
    assert e.book_ids == ("A", "B", "C") and e.capital_inr == 1_000_000
    assert e.books["B"].advisor == "typed_veto" and e.books["B"].threshold == 0.6
    assert e.books["C"].role == "veto" and e.strategies.shadow == ("breakout", "trend_following")
    config = engine_config(e, environment="paper", limits=load_risk_limits())
    assert config.books == ("A", "B", "C") and config.starting_cash == 1_000_000
    assert (config.lifecycle.entry_window_start.isoformat(), config.policy.max_hold_days) == (
        "09:20:00",
        10,
    )
    assert config.decision is not None and config.decision.enabled == ("momentum", "mean_reversion")


@pytest.mark.parametrize(
    ("patch", "match"),
    [({"learning_injection": True}, "learning_injection"),
     ({"direction": "long_short"}, "direction"),
     ({"entry_window": "09:45-09:20"}, "entry_window"),
     ({"books": {"A": {"advisor": "llm_veto"}}}, "role"),
     ({"books": {"A": {"advisor": "none", "threshold": 0.5}}}, "no threshold"),
     ({"strategies": {"enabled": ["momentum"], "shadow": ["momentum"]}}, "both"),
     ({"colour": "blue"}, "colour")],
)  # fmt: skip
def test_a_bad_experiment_fails_startup(tmp_path, patch, match):
    import yaml

    data = yaml.safe_load(DEFAULT_EXPERIMENT_PATH.read_text(encoding="utf-8"))
    data.update(patch)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_experiment(path)


def test_the_experiment_cannot_enable_what_risk_forbids():
    e = load_experiment().model_copy(
        update={
            "strategies": ExperimentConfig.model_fields["strategies"].annotation(
                enabled=("breakout",)
            )
        }
    )  # type: ignore[misc]
    with pytest.raises(ConfigError, match="RISK_ENABLED_STRATEGIES"):
        engine_config(e, environment="paper", limits=load_risk_limits())


def test_advisors_come_from_the_experiment_and_abstain_explicitly_when_unavailable():
    from src.domain.types import AdvisorKind

    e, sink = load_experiment(), RecordingSink(ReplayClock(at(9, 0)))
    common: dict[str, Any] = {"sink": sink, "clock": ReplayClock(at(9, 0)), "calendar": CAL,
                              "events": lambda key: [], "regime": lambda: None}  # fmt: skip
    bare = build_advisors(e, cascade=None, router=None, **common)
    assert bare["A"] is None
    assert isinstance(bare["B"], AbstainAdvisor) and bare["B"].reason == "no_decision_model"
    assert isinstance(bare["C"], AbstainAdvisor) and bare["C"].reason == "llm_role_veto_disabled"
    assert bare["C"].kind is AdvisorKind.LLM_VETO

    class Router:
        roles = {"veto": RoleConfig("veto", (ModelSpec.parse("groq:llama-3.3-70b-versatile"),))}

    full = build_advisors(e, cascade=object(), router=Router(), **common)  # type: ignore[arg-type]
    assert isinstance(full["B"], TypedVetoAdvisor) and full["B"].threshold == 0.6
    assert isinstance(full["C"], LLMVetoAdvisor) and full["C"].threshold == 0.6


async def test_an_abstaining_book_records_why():
    sink = RecordingSink(ReplayClock(at(9, 0)))
    from src.domain.types import AdvisorKind
    from tests.test_llm_prompts import one_proposal

    proposal, features = await one_proposal(None)
    advisor = AbstainAdvisor(kind=AdvisorKind.TYPED_VETO, book_id="B", reason="no_decision_model",
                             sink=sink)  # fmt: skip
    assert await advisor.review(proposal, features) is Verdict.ABSTAIN
    assert [e.type for e in sink.events] == [
        "AdvisorRequested",
        "AdvisorFallback",
        "AdvisorVerdict",
    ]


# --- 8.2 paired books on one engine ----------------------------------------------------------------


def two_signal_tape(root: Path) -> None:
    """INFY rallies to its target (a winner); TCS falls through its stop (a known loser)."""
    tape = TapeWriter(root)
    tape.add_bars(history(INFY.key, 3, 1000.0) + history(TCS.key, 21, 4000.0)
                  + history("NSE:INDEX:NIFTY50", 5, 25_000.0), recorded_on=DAY)  # fmt: skip
    infy = [(at(9, 15), 1001.0), *ramp(at(9, 30), at(13, 0), 1002.0, 1070.0), (at(13, 1), 1070.0)]
    tcs = [(at(9, 15), 4001.0), *ramp(at(9, 30), at(12, 0), 4000.0, 3780.0), (at(12, 1), 3780.0)]
    tape.add_quotes(session_quotes(INFY.key, infy, 1000.0))
    tape.add_quotes(session_quotes(TCS.key, tcs, 4000.0))
    tape.flush()


class VetoInstrument:
    """B's advisor for the test: vetoes one named instrument (the known loser)."""

    def __init__(self, key: str) -> None:
        self.key, self.reviewed = key, []

    async def review(self, proposal: Proposal, features: Features) -> Verdict:
        self.reviewed.append(proposal.intent.instrument.key)
        return Verdict.VETO if proposal.intent.instrument.key == self.key else Verdict.APPROVE


@contextmanager
def three_books(tmp_path: Path) -> Iterator[tuple[Engine, ReplayClock]]:
    two_signal_tape(tmp_path / "tape")
    with EventStore(tmp_path / "rq.db") as store:
        clock = ReplayClock(at(8, 50))
        engine = build_engine(
            config=EngineConfig(environment="paper", books=("A", "B", "C")), clock=clock,
            calendar=CAL, store=store,
            quotes=TapeQuoteSource(read_quotes(tmp_path / "tape", DAY), clock=clock),
            history=TapeHistorySource(read_bars(tmp_path / "tape", DAY)),
            universe=[INFY, TCS], limits=load_risk_limits(),
            advisors={"B": VetoInstrument(TCS.key)},
        )  # fmt: skip
        yield engine, clock


async def run(engine: Engine, clock: ReplayClock) -> int:
    task = asyncio.create_task(engine.run())
    while not task.done():
        await clock.advance(30)
    return task.result()


async def test_books_trade_in_isolation_and_share_the_decision_id(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        assert await run(engine, clock) == 0
        store = engine.store
        signals = {e.payload.signal.instrument_key: e.payload.signal
                   for e in store.read(types=["SignalGenerated"])
                   if e.payload.signal.strategy == "momentum"}  # fmt: skip
        assert set(signals) == {INFY.key, TCS.key}

        trades = store.query("SELECT book_id, instrument_key, exit_reason, net_pnl FROM trades")
        by_book = {b: {(t["instrument_key"], t["exit_reason"]) for t in trades if t["book_id"] == b}
                   for b in ("A", "B", "C")}  # fmt: skip
        assert by_book["A"] == by_book["C"] == {(INFY.key, "target"), (TCS.key, "stop")}
        assert by_book["B"] == {(INFY.key, "target")}  # B vetoed TCS

        # One decision_id per signal, shared by every book's intent; book_id on every order.
        orders = [e.payload.order for e in store.read(types=["OrderSubmitted"])]
        for key, signal in signals.items():
            entries = [
                o
                for o in orders
                if o.intent.instrument.key == key and o.intent.kind.value == "open"
            ]
            assert {o.intent.decision_id for o in entries} == {signal.decision_id}
            assert len({o.client_order_id for o in entries}) == len(entries)  # own orders per book
        dispositions = {(d.book_id, d.instrument_key, d.disposition)
                        for e in store.read(types=["SignalDisposition"])
                        if (d := e.payload).strategy == "momentum"}  # fmt: skip
        assert ("B", TCS.key, Disposition.VETOED) in dispositions
        assert ("A", TCS.key, Disposition.SUBMITTED) in dispositions
        assert {d for d in dispositions if d[0] == "C"} == {("C", INFY.key, Disposition.SUBMITTED),
                                                             ("C", TCS.key, Disposition.SUBMITTED)}  # fmt: skip

        # Each book has its own cash, positions, risk state and kill switches.
        assert {b.oms.book.quantity(INFY.key, Product.CNC) for b in engine.books.values()} == {0}
        assert len({id(b.switches) for b in engine.books.values()}) == 3
        assert len({b.tracker.state.entries for b in engine.books.values()} | {0}) >= 2  # type: ignore[union-attr]


async def test_acceptance_b_beats_a_by_exactly_the_vetoed_loser(tmp_path):
    with three_books(tmp_path) as (engine, clock):
        await run(engine, clock)
        mtm = {
            e.payload.book_id: e.payload.equity for e in engine.store.read(types=["MarkToMarket"])
        }
        (loss,) = engine.store.query(
            "SELECT net_pnl FROM trades WHERE book_id = 'A' AND instrument_key = ?", (TCS.key,)
        )
        loser = Decimal(loss["net_pnl"])
        assert loser < 0
        ai_cost = Decimal(0)  # the test advisor is local; the daily report nets LLM spend (M8.4)
        assert mtm["B"] - mtm["A"] == -loser - ai_cost
        assert mtm["C"] == mtm["A"]  # C abstained: identical to A
