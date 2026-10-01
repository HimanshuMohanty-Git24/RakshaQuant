"""
Decision-model adapters (plan M7.2).

* :class:`LayaLocal` - Laya in-process (``laya.load``), on a **dedicated single-thread executor**
  so a CPU forward pass never blocks the event loop and never runs concurrently with itself.
  ``laya`` is the optional ``decision-local`` extra; it is imported only when first used.
* :class:`SystemOneHTTP` - the ``/v1/systemone`` wire protocol, spoken by TypeSafe's Jev and by
  ``laya-serve`` (verified wire-compatible, 2026-10-02): ``POST {state, model, questions}``,
  Bearer auth when a key is set, 15 s timeout, retries on 429/529 honouring ``Retry-After``
  (≤ 30 s), at most 3 attempts. :func:`jev_remote` and :func:`laya_http` configure it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2

from src.decision_models.base import (
    Answer,
    DecisionModelError,
    Question,
    parse_answers,
)

CONTEXT_TOKENS = {"english": 512, "multilingual": 1024, "typed-decisions": 1024}
SUBFOLDERS = {"english": None, "multilingual": "multilingual", "typed-decisions": "typed-decisions"}
JEV_URL = "https://api.typesafe.ai"
JEV_MODEL = "jev-1.13.0"


class LayaLocal:
    name = "laya"

    def __init__(self, *, checkpoint: str = "english", device: str = "cpu",
                 cache_dir: str | None = None, agent: Any = None) -> None:  # fmt: skip
        if checkpoint not in SUBFOLDERS:
            raise ValueError(f"unknown Laya checkpoint {checkpoint!r} ({', '.join(SUBFOLDERS)})")
        self._checkpoint = checkpoint
        self.device = device
        self._cache_dir = cache_dir
        self._agent = agent  # injected in tests
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya")

    @property
    def checkpoint(self) -> str:
        return self._checkpoint

    @property
    def context_tokens(self) -> int | None:
        return CONTEXT_TOKENS[self._checkpoint]

    def _load(self) -> Any:
        if self._agent is None:
            if self._cache_dir:
                os.environ.setdefault("HF_HOME", self._cache_dir)
            try:
                import laya
            except ImportError as exc:  # the optional extra is not installed
                raise DecisionModelError(
                    "laya is not installed (uv sync --extra decision-local)"
                ) from exc
            self._agent = laya.load("convaiinnovations/laya",
                                    subfolder=SUBFOLDERS[self._checkpoint], device=self.device)  # fmt: skip
        return self._agent

    async def warm(self) -> None:
        """Load the checkpoint now (it is otherwise loaded by the first decision)."""
        await asyncio.get_running_loop().run_in_executor(self._executor, self._load)

    async def decide(
        self, state: Mapping[str, str], questions: Mapping[str, Question]
    ) -> dict[str, Answer]:
        wire = {name: q.wire() for name, q in questions.items()}

        def run() -> Any:
            return self._load().predict(dict(state), wire)

        try:
            payload = await asyncio.get_running_loop().run_in_executor(self._executor, run)
        except DecisionModelError:
            raise
        except Exception as exc:
            raise DecisionModelError(f"laya failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise DecisionModelError("laya returned no mapping")
        return parse_answers(payload, questions, f"laya:{self._checkpoint}")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SystemOneHTTP:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 15.0,
        max_attempts: int = 3,
        max_retry_after_s: float = 30.0,
        context_tokens: int | None = None,
        client: httpx2.AsyncClient | None = None,
        sleep: Any = None,
    ) -> None:
        self.name = name
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._model = model
        self._key = api_key
        self._timeout = timeout_s
        self._attempts = max(1, max_attempts)
        self._max_wait = max_retry_after_s
        self._context = context_tokens
        self._client = client or httpx2.AsyncClient(timeout=timeout_s)
        self._sleep = sleep or asyncio.sleep

    @property
    def checkpoint(self) -> str:
        return self._model

    @property
    def context_tokens(self) -> int | None:
        return self._context

    async def decide(
        self, state: Mapping[str, str], questions: Mapping[str, Question]
    ) -> dict[str, Answer]:
        body = {"state": dict(state), "model": self._model,
                "questions": {name: q.wire() for name, q in questions.items()}}  # fmt: skip
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._client.post(self._url, json=body, headers=headers,
                                                   timeout=self._timeout)  # fmt: skip
            except httpx2.HTTPError as exc:
                raise DecisionModelError(f"{self.name} unreachable: {type(exc).__name__}") from exc
            if response.status_code in (429, 529):
                if attempt == self._attempts:
                    break
                await self._sleep(self._backoff(response, attempt))
                continue
            if response.status_code != 200:
                raise DecisionModelError(f"{self.name} returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise DecisionModelError(f"{self.name} returned invalid JSON") from exc
            return parse_answers(payload, questions, f"{self.name}:{self._model}")
        raise DecisionModelError(f"{self.name} still rate-limited after {self._attempts} attempts")

    def _backoff(self, response: httpx2.Response, attempt: int) -> float:
        header = response.headers.get("retry-after")
        try:
            wait = float(header) if header is not None else 2.0**attempt
        except ValueError:
            wait = 2.0**attempt
        return max(0.0, min(wait, self._max_wait))

    async def close(self) -> None:
        await self._client.aclose()


def jev_remote(api_key: str, *, model: str = JEV_MODEL, base_url: str = JEV_URL,
               client: httpx2.AsyncClient | None = None, **kw: Any) -> SystemOneHTTP:  # fmt: skip
    return SystemOneHTTP(name="jev", base_url=base_url, model=model, api_key=api_key,
                         context_tokens=64_000, client=client, **kw)  # fmt: skip


def laya_http(base_url: str, *, checkpoint: str = "english",
              client: httpx2.AsyncClient | None = None, **kw: Any) -> SystemOneHTTP:  # fmt: skip
    return SystemOneHTTP(name="laya", base_url=base_url, model=checkpoint,
                         context_tokens=CONTEXT_TOKENS.get(checkpoint), client=client, **kw)  # fmt: skip
