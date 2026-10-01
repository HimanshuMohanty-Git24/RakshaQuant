"""
The hallucination check (audit §J.2): every claim an LLM makes must cite an ``evidence_ref`` that
names a key in the input it was given (``features.rsi_14``, ``events[2]``, ``events[2].title``).
A reference to anything that was not in the input fails the check, and the caller turns the
verdict into ABSTAIN.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_INDEX = re.compile(r"\[(\d+)\]")


def key_paths(data: Any, prefix: str = "") -> set[str]:
    """Every addressable path in ``data``: ``a``, ``a.b``, ``a[0]``, ``a[0].c``."""
    paths: set[str] = set()
    if isinstance(data, Mapping):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= key_paths(value, path)
    elif isinstance(data, list | tuple):
        for i, value in enumerate(data):
            path = f"{prefix}[{i}]"
            paths.add(path)
            paths |= key_paths(value, path)
    return paths


def normalise(ref: str) -> str:
    """Tolerate ``events.2`` for ``events[2]`` and surrounding whitespace/backticks."""
    ref = ref.strip().strip("`").strip()
    return re.sub(r"\.(\d+)(?=\.|$)", r"[\1]", ref)


def unsupported(refs: Iterable[str], data: Any) -> list[str]:
    """The references that point at nothing in ``data`` (empty = every claim is grounded)."""
    known = key_paths(data)
    return [ref for ref in refs if normalise(ref) not in known]
