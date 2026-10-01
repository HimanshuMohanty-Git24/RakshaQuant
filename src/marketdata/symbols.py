"""Mapping between NSE instruments and Yahoo Finance tickers."""

NIFTY_50_INDEX_TICKER = "^NSEI"


def yahoo_ticker(symbol: str) -> str:
    """``INFY`` -> ``INFY.NS`` (NSE cash market on Yahoo)."""
    if not symbol or symbol != symbol.strip():
        raise ValueError(f"bad NSE symbol {symbol!r}")
    return f"{symbol}.NS"
