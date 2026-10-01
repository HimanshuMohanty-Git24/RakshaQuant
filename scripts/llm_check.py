"""
LLM connectivity check (plan M6): one tiny schema-validated call per **enabled** role, through the
real router (fallbacks, budgets, breakers; the reply cache is bypassed), recorded as ``LLMCall``
events in the environment's store. Prints, per role::

    role | provider:model | ok / fail (outcome) | latency | tokens in/out

It never prints keys. Exit 0 when every enabled role answered, 1 when one did not, 2 when the
LLM configuration is invalid (unknown provider, missing key for an enabled role).

    uv run python scripts/llm_check.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from pydantic import BaseModel

from src.config import get_settings
from src.domain.base import EventPayload
from src.domain.clock import WallClock
from src.domain.events import Event, LLMCall
from src.llm.prompts.base import PromptTemplate
from src.llm.registry import validate_roles
from src.llm.setup import build_router
from src.ops.exit_codes import ExitCode
from src.ops.process import run_entry_point
from src.store.event_store import EventStore
from src.store.sink import StoreSink


class Ping(BaseModel):
    ok: bool
    schema_version: int


PING = PromptTemplate(
    name="ping",
    version=1,
    system="You are a connectivity check for a trading system's LLM layer.",
    instructions='Return exactly this JSON object: {"ok": true, "schema_version": 1}',
)


class _Tee:
    """Records LLMCall payloads in memory while the store keeps the events."""

    def __init__(self, sink: StoreSink) -> None:
        self._sink = sink
        self.calls: list[LLMCall] = []

    def emit(
        self, payload: EventPayload, *, source: str | None = None, cycle_id: str | None = None
    ) -> Event:
        if isinstance(payload, LLMCall):
            self.calls.append(payload)
        return self._sink.emit(payload, source=source, cycle_id=cycle_id)


async def check() -> int:
    settings = get_settings()
    roles = [r for r in validate_roles(settings).values() if r.enabled]
    if not roles:
        print("No LLM roles are enabled. Set LLM_ROLE_<ROLE>=provider:model in .env.")
        return ExitCode.OK
    clock = WallClock()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    with EventStore(settings.db_path) as store:
        tee = _Tee(StoreSink(store, clock, "llm_check"))
        router = build_router(settings, clock=clock, sink=tee, store=store, use_cache=False)
        for role in roles:
            before = len(tee.calls)
            started = time.perf_counter()
            result = await router.complete(
                role.role, PING.render({"check": {"role": role.role}}), Ping,
                prompt_version=PING.prompt_version,
            )  # fmt: skip
            latency = (time.perf_counter() - started) * 1000
            attempts = tee.calls[before:]
            last = attempts[-1] if attempts else None
            model = result.model or (role.chain[0].spec if role.chain else "-")
            status = "ok" if result.ok and result.parsed and result.parsed.ok else (
                f"fail ({result.outcome})"
            )  # fmt: skip
            tokens = f"{last.tokens_in}/{last.tokens_out}" if last else "-"
            print(f"{role.role} | {model} | {status} | {latency:.0f} ms | {tokens}")
            failures += status != "ok"
    return ExitCode.OK if failures == 0 else ExitCode.CRASH


def main() -> int:
    return asyncio.run(check())


if __name__ == "__main__":
    run_entry_point("llm_check", main, console_log_level="WARNING")
