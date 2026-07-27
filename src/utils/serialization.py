"""
Serialization helpers.

The LangGraph pipeline checkpoints ``TradingState`` through msgpack (``MemorySaver`` uses
``JsonPlusSerializer``). msgpack's type check is *exact*: a ``numpy.float64`` (even though it
subclasses ``float``) or a ``numpy.int64`` reaching that boundary raises
``TypeError: Type is not msgpack serializable: numpy.float64`` and crashes the whole cycle.

Values computed with pandas / scikit-learn (the ML prediction agent, P&L derived from
quote prices, drawdown, etc.) are numpy scalars unless explicitly coerced. ``to_native``
walks a nested structure and converts every numpy scalar/array to the equivalent native
Python type so the state is always msgpack-safe.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def to_native(obj: Any) -> Any:
    """
    Recursively convert numpy scalars/arrays inside ``obj`` to native Python types.

    - numpy scalar (``np.generic``, e.g. ``float64``/``int64``/``bool_``) -> ``.item()``
    - numpy array -> ``list`` (recursively native, via ``tolist``)
    - dict / list / tuple -> same container with each element converted
    - everything else is returned unchanged

    Safe to call on already-native data (it is a no-op for plain Python values).
    """
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return to_native(obj.tolist())
    if isinstance(obj, dict):
        return {key: to_native(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(item) for item in obj]
    return obj
