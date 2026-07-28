# RakshaQuant Web Console — frontend

A **Neo-Terminal Trading Console**: a Bloomberg-dense, LLM-observability-style monitoring +
control UI for RakshaQuant. Built with **React + Vite + TypeScript + Tailwind**, monospace-first,
dark-only, WCAG 2.2 AA, keyboard-first.

It is a thin presentation layer — it renders the live `TradingStats` snapshot and per-cycle
observability traces streamed by the FastAPI backend (`src/web`) over a WebSocket. All trading
logic lives in the shared Python engine.

## Develop

Run the backend in dev mode (enables CORS for the Vite dev server), then Vite:

```bash
# terminal 1 — backend (from repo root)
uv run python scripts/run_live_trading.py --mode web --dev --demo

# terminal 2 — frontend
cd frontend
npm install
npm run dev          # http://localhost:5173  (proxies /api + /ws to :8000)
```

## Build (served by FastAPI)

```bash
cd frontend
npm install
npm run build        # emits frontend/dist
```

Then `--mode web` serves the built SPA directly:

```bash
uv run python scripts/run_live_trading.py --mode web
# open http://127.0.0.1:8000
```

## Design tokens

The entire look (color, type, density) is centralized: CSS variables in
[`src/index.css`](src/index.css), surfaced to Tailwind as semantic names in
[`tailwind.config.ts`](tailwind.config.ts). Retune the palette or density in one place.

## Keyboard

`j`/`k` move through cycles · `Enter` open a span · `/` filter spans · `:` command input ·
`Esc` close · `g` then `t`/`f` jump to trace/feed · `?` cheatsheet.
