"""
Web console for RakshaQuant — the browser front end of the dual-mode CLI/web app.

This package is a thin presentation + control layer over the existing engine: it runs the
*same* :func:`src.live.session.run_trading_session` loop inside the server process and
streams the shared :class:`TradingStats` snapshot (plus per-cycle observability traces) to
the browser over a WebSocket. It never re-implements trading logic.

FastAPI/uvicorn are optional (``pip install .[web]`` / ``uv sync --extra web``); this module
is only imported when the app is launched with ``--mode web``.
"""
