"""
RakshaQuant: a paper-trading platform for NSE equities (Platform v2).

A deterministic engine trades paired books on the same signals - one without an advisor, one
vetoed by a typed decision model, one by an LLM - on a simulated broker, recording every fact
in an event store. See ``docs/architecture.md``.
"""

__version__ = "0.1.0"
