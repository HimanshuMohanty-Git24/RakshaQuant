"""Plan M4.2: the RiskEngine is the OMS's gate - every order, decision persisted before routing."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from src.config.limits import load_risk_limits
from src.domain.calendar import get_calendar
from src.domain.types import (
    Instrument,
    IntentKind,
    IntentReason,
    IntentSource,
    KillSwitchState,
    MarketDataSource,
    OrderIntent,
    Product,
    ReasonCode,
    RiskOutcome,
    Side,
)
from src.oms.exit_manager import ExitPolicy
from src.risk.engine import RiskEngine
from src.risk.gate import RiskGate
from src.risk.kill_switch import KillSwitchRegistry
from src.risk.snapshot import MarketFacts
from src.risk.state import DailyRiskTracker
from src.store.kv import KVStateStore
from src.store.sink import StoreSink
from src.utils.market_time import IST
from tests.oms_harness import INFY, Harness, at, harness
from tests.test_oms_exit_manager import ATR, live_orders, manager

R = ReasonCode
TCS = Instrument.nse_equity("TCS", sector="IT")


def wire(h: Harness, *, halt_file=None, environment: str = "paper", **limits) -> RiskGate:
    limits_ = load_risk_limits(limits or None)
    sink = StoreSink(h.store, h.clock, "risk")
    tracker = DailyRiskTracker(book_id="A", limits=limits_, clock=h.clock, sink=sink,
                               state_store=KVStateStore(h.store, "A", "daily_risk"))  # fmt: skip
    switches = KillSwitchRegistry(book_id="A", limits=limits_, clock=h.clock, sink=sink,
                                  state_store=KVStateStore(h.store, "A", "kill_switches"),
                                  halt_file=halt_file)  # fmt: skip
    h.oms.add_event_listener(tracker.on_event)
    tracker.start_day(h.book.cash)

    def market(instrument: Instrument) -> MarketFacts:
        quote = h.last_quote if instrument.key == INFY.key else None
        return MarketFacts(quote=quote, atr=ATR, adv_shares=5_000_000.0,
                           data_source=None if quote is None else quote.source)  # fmt: skip

    gate = RiskGate(engine=RiskEngine(limits_), oms=h.oms, tracker=tracker, switches=switches,
                    calendar=get_calendar(), clock=h.clock, sink=sink, market=market,
                    environment=environment, instruments={INFY.key: INFY})  # fmt: skip
    h.oms.use_gate(gate)
    return gate


def entry(instrument: Instrument = INFY, *, stop: str = "960", target: str = "1062",
          price: str = "1000", leg: str = "entry", qty: int | None = None) -> OrderIntent:  # fmt: skip
    return OrderIntent(
        intent_id=f"A:momentum:{instrument.key}:2026-10-02:{leg}", decision_id="d1", book_id="A",
        strategy="momentum", instrument=instrument, side=Side.BUY, kind=IntentKind.OPEN,
        reduce_only=False,
        quantity=qty, product=Product.CNC, decision_price=Decimal(price),
        stop_price=Decimal(stop), target_price=Decimal(target), decision_ts=at(9, 25),
        reason=IntentReason.ENTRY, source=IntentSource.SIGNAL_ENGINE,
    )  # fmt: skip


async def test_an_entry_is_sized_and_its_decision_is_persisted_before_routing(tmp_path):
    with harness(tmp_path, gate=None) as h:
        wire(h)
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(entry())  # unsized: the engine sizes it
        assert result.status == "SUBMITTED" and result.order is not None
        assert result.order.quantity == 100  # 10% of 10L at 1,000 (risk 2% would allow 500)
        events = h.store.read(types=["RiskDecision", "OrderSubmitted"])
        assert [e.type for e in events] == ["RiskDecision", "OrderSubmitted"]
        decision = events[0].payload
        assert decision.client_order_id == result.order.client_order_id
        assert decision.outcome is RiskOutcome.APPROVED
        assert [r.code for r in decision.reasons] == [R.ORD_NOTIONAL_MAX]  # what set the size
        (row,) = h.store.query("SELECT outcome, qty_approved FROM decisions")
        assert (row["outcome"], row["qty_approved"]) == ("APPROVED", 100)


async def test_a_rejected_order_never_reaches_the_broker(tmp_path):
    with harness(tmp_path, gate=None) as h:
        wire(h)
        await h.oms.start()
        h.quote(1000.0)
        result = await h.oms.submit(entry(stop="1001"))  # audit F-12: a BUY stop above price
        assert result.status == "BLOCKED" and "ORD_STOP_WRONG_SIDE" in result.message
        assert h.events("OrderSubmitted") == [] and h.oms.orders == {}
        (decision,) = h.events("RiskDecision")
        assert decision.outcome is RiskOutcome.REJECTED and decision.qty_approved == 0


async def test_acceptance_the_halt_file_blocks_the_next_submit(tmp_path):
    halt = tmp_path / "HALT"
    with harness(tmp_path, gate=None) as h:
        wire(h, halt_file=halt)
        await h.oms.start()
        h.quote(1000.0)
        halt.touch()  # no monitor tick in between
        result = await h.oms.submit(entry())
        assert result.status == "BLOCKED" and "SYS_KILL_GLOBAL" in result.message
        (decision,) = h.events("RiskDecision")
        assert decision.outcome is RiskOutcome.HALTED
        assert decision.kill_state.global_state is KillSwitchState.HALT_NEW


async def test_acceptance_saturday_proposals_are_session_closed(tmp_path):
    saturday = datetime.combine(date(2026, 10, 3), time(10, 0), IST)
    with harness(tmp_path, gate=None, start=saturday) as h:
        wire(h)
        h.quote(1000.0)
        for symbol in ("INFY", "TCS", "ITC", "SBIN", "LT", "HDFCBANK", "RELIANCE", "AXISBANK"):
            instrument = INFY if symbol == "INFY" else Instrument.nse_equity(symbol)
            result = await h.oms.submit(entry(instrument, leg=f"e-{symbol}"))
            assert result.status == "BLOCKED" and "SYS_SESSION_CLOSED" in result.message


async def test_outside_calendar_coverage_nothing_trades(tmp_path):
    monday_2027 = datetime.combine(date(2027, 1, 4), time(10, 0), IST)
    with harness(tmp_path, gate=None, start=monday_2027) as h:
        wire(h)
        h.quote(1000.0)
        result = await h.oms.submit(entry())
        assert result.status == "BLOCKED" and "SYS_HOLIDAY" in result.message


async def test_working_entries_hold_their_capacity(tmp_path):
    with harness(tmp_path, gate=None) as h:
        wire(h, max_positions=1)
        await h.oms.start()
        h.quote(1000.0)
        first = await h.oms.submit(entry())
        assert first.status == "SUBMITTED"  # working: no quote since, so not filled yet
        again = await h.oms.submit(entry(leg="entry2"))
        assert again.status == "BLOCKED" and "PF_DUPLICATE" in again.message
        other = await h.oms.submit(entry(TCS, leg="tcs"))
        assert other.status == "BLOCKED" and "PF_MAX_POSITIONS" in other.message


async def test_exits_and_stops_go_through_the_gate_too(tmp_path):
    with harness(tmp_path, gate=None) as h:
        wire(h)
        exits = manager(h, ExitPolicy(k_stop_atr=1.0, k_target_atr=2.0, k_trail_atr=None))
        await h.oms.start()
        h.quote(1000.0)
        first = entry()
        exits.expect_entry(first, atr=ATR, signal_bar_date=date(2026, 10, 2))
        await h.oms.submit(first)
        h.quote(1000.0)  # fills; the exit manager places its SL-M stop
        assert h.last_quote is not None
        await exits.on_quote(h.last_quote)
        assert [i.split(":")[-1] for i in live_orders(h)] == ["stop-2026-10-05-v1"]
        kinds = [(d.kind, d.outcome) for d in h.events("RiskDecision")]
        assert kinds == [(IntentKind.OPEN, RiskOutcome.APPROVED),
                         (IntentKind.CLOSE, RiskOutcome.APPROVED)]  # fmt: skip


async def test_simulated_prices_cannot_open_outside_demo(tmp_path):
    with harness(tmp_path, gate=None) as h:
        wire(h)
        h.quote(1000.0)
        assert h.last_quote is not None
        h.last_quote = h.last_quote.model_copy(update={"source": MarketDataSource.SIMULATED})
        result = await h.oms.submit(entry())
        assert result.status == "BLOCKED" and "SYS_DATA_SIMULATED" in result.message


async def test_preview_records_nothing(tmp_path):
    with harness(tmp_path, gate=None) as h:
        gate = wire(h)
        h.quote(1000.0)
        decision = gate.preview(entry())
        assert decision.outcome is RiskOutcome.APPROVED and decision.qty_approved == 100
        assert h.events("RiskDecision") == [] and h.oms.orders == {}
