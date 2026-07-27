"""
Safe display-formatting helpers.

Many numeric fields in the pipeline are *optional* — indicator values are ``None`` during
the warm-up window (``_safe_float`` maps NaN/inf to ``None``), so a symbol can produce a
signal while, say, ADX is still ``None``. Formatting such a value with a numeric spec
(``f"{value:.1f}"``) raises ``TypeError: unsupported format string passed to
NoneType.__format__`` and crashes the trading cycle. ``fmt_optional`` renders a placeholder
instead of raising.
"""

from __future__ import annotations

from typing import Any


def fmt_optional(value: Any, spec: str = "", default: str = "N/A") -> str:
    """
    Format ``value`` with ``spec``, returning ``default`` when it is ``None`` (or not
    formattable with a numeric spec).

    >>> fmt_optional(42.1234, ".1f")
    '42.1'
    >>> fmt_optional(None, ".1f")
    'N/A'
    """
    if value is None:
        return default
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return default
