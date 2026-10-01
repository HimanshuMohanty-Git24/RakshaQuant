"""The client contract (plan M6) and the JSON-schema helpers both client kinds share."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.registry import ModelSpec
from src.llm.types import ClientReply, LLMInvalidOutputError, Message

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    async def complete(
        self,
        model: ModelSpec,
        messages: Sequence[Message],
        schema: type[BaseModel],
        *,
        effort: str | None = None,
        max_tokens: int = 4096,
        private: bool = True,
    ) -> ClientReply:
        """One schema-validated call. Raises an :class:`~src.llm.types.LLMError` subclass.

        ``private`` marks prompts with non-public context (positions, decisions): providers
        that can route to third parties are told not to retain or train on them.
        """
        ...


def strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema made strict: every object closed (``additionalProperties:
    false``) with all properties required, and no defaults or titles."""
    strict: dict[str, Any] = _strict(schema.model_json_schema())
    return strict


def _strict(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("default", "title"):  # schema keywords, never property names
                continue
            if key in ("properties", "$defs") and isinstance(value, dict):
                out[key] = {name: _strict(sub) for name, sub in value.items()}
            else:
                out[key] = _strict(value)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(node, list):
        return [_strict(v) for v in node]
    return node


def schema_instruction(schema: type[BaseModel]) -> str:
    """For providers without schema enforcement: the schema, stated in the system prompt."""
    return (
        "Reply with a single JSON object and nothing else. It must validate against this JSON "
        f"schema:\n{json.dumps(strict_schema(schema), sort_keys=True)}"
    )


def parse_json(text: str, schema: type[T]) -> T:
    """Validate a reply; tolerate a fenced block, never repair content."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        return schema.model_validate_json(body.strip())
    except (ValidationError, ValueError) as exc:
        raise LLMInvalidOutputError(f"reply does not match {schema.__name__}: {exc}") from exc
