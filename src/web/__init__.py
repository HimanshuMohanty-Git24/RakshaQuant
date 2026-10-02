"""
Web console for RakshaQuant - the browser front end (``--mode web``).

A thin presentation + control layer over the engine: the server runs the same v2 engine
(:mod:`src.engine.live`) in its process, serves REST projections and a WebSocket event stream
from its own read connection to the event store, and never re-implements trading logic.

FastAPI/uvicorn are optional (``uv sync --extra web``) and only :mod:`src.web.server` imports
them. The read model (:mod:`src.web.queries`, :mod:`src.web.models`) is FastAPI-free: the CLI
dashboard uses it too.
"""
