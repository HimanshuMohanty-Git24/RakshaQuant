# RakshaQuant console (frontend v2)

The browser terminal for the v2 engine: eight screens over the web API (`src/web`), built to the
design spec in `docs/plan/2026-10-01-platform-v2-plan.md` §6. It only *formats* server
projections: it never computes money, and nothing is fabricated outside the demo.

React 18 + Vite + strict TypeScript, TanStack Query (REST projections) and a zustand store fed by
the WebSocket event stream, TanStack Table + Virtual, Radix primitives, cmdk, lightweight-charts
(lazy-loaded), IBM Plex. Screenshots of every screen: `docs/ui/screens/`.

## Requirements

- **Node `^20.19` or `>=22.12`** (Vite 8). `frontend/.npmrc` pins the https npm registry.
- The backend's `web` extra: `uv sync --extra web` (from the repo root).

## Run

```bash
# production build, served by the backend at http://127.0.0.1:8000
npm ci && npm run build
cd .. && uv run python scripts/run_live_trading.py --mode web            # paper session
cd .. && uv run python scripts/run_live_trading.py --mode web --demo     # the demo tape
```

The server prints a one-time link ending in `#token=…`: open that link. The console keeps the
token for the tab and removes it from the address bar; a restart issues a new token.

For development, start the backend with `--dev` (it then accepts the Vite origin) and run
`npm run dev` (`http://localhost:5173`, proxying `/api` and `/ws`); open it with the printed
`#token=…` fragment appended.

## Check

```bash
npm run typecheck     # the app (browser types) and the tests/tooling (Node types)
npm run lint          # ESLint with react-hooks
npm test              # Vitest: formatters, components, the stream reducer, the §6.2 design rules
npm run build && npm run check:bundle   # initial-route JS must stay under 250 KB gzipped
npm run e2e           # Playwright against the real server replaying the demo tape
```

`npm run e2e` starts `run_live_trading.py --mode web --demo` itself, waits for the demo session to
finish, and runs every screen, the Decision Inspector lineage, HALT/RESUME, auth, an axe scan and
a 5,000-row scrolling test. Without a downloaded browser, `PW_CHANNEL=msedge npm run e2e` uses
the installed Edge. `RQ_SCREENSHOTS=1` writes the screenshots to `docs/ui/screens/`.

## The API contract

`src/api/types.gen.ts` is generated from the backend's OpenAPI document; never edit it:

```bash
cd .. && uv run --extra web python scripts/export_openapi.py && cd frontend && npm run gen:api
```

CI regenerates both and fails on any difference.
