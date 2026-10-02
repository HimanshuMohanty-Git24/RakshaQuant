// 7. Market (§6.5): the universe with live quotes, signals and event flags; a daily chart with
// this system's entries, exits and announcements; and the typed announcements feed.

import { useQuery } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { useMemo } from "react";
import { useSearchParams } from "react-router";

import { get, type Schemas } from "../api/client";
import { keys } from "../api/queries";
import { duration } from "../lib/format";
import { useStream } from "../lib/stream";
import { Badge } from "../ui/Badge";
import { Chart } from "../ui/Chart";
import { DataTable, type Columns } from "../ui/DataTable";
import { DateIST, Pct, Price, TimeIST } from "../ui/Num";
import { Empty, Panel } from "../ui/Panel";

type Watch = Schemas["WatchRow"];

const w = createColumnHelper<Watch>();
const WATCH_COLUMNS: Columns<Watch> = [
  w.accessor("symbol", { header: "Symbol", size: 96, cell: (c) => <span className="font-medium">{c.getValue()}</span> }),
  w.accessor("ltp", { header: "LTP", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  w.accessor("change_pct", { header: "Chg", size: 70, meta: { align: "right" }, cell: (c) => <Pct value={c.getValue()} pnl /> }),
  w.accessor("quote_age_s", { header: "Age", size: 70, meta: { align: "right" }, cell: (c) => <span className="text-fg-1">{c.getValue() == null ? "—" : duration(c.getValue())}</span> }),
  w.accessor((r) => r.signals_today.join(", "), { id: "signals", header: "Signals today", size: 160, cell: (c) => <span className="text-fg-1">{c.getValue() || "—"}</span> }),
  w.accessor((r) => r.events.join(", "), { id: "events", header: "Events", size: 130, cell: (c) => (c.getValue() ? <Badge tone="warn">{c.getValue()}</Badge> : <span className="text-fg-2">—</span>) }),
  w.accessor("held", { header: "Held", size: 50, cell: (c) => (c.getValue() ? "yes" : "—") }),
  w.accessor("sector", { header: "Sector", size: 150, cell: (c) => <span className="text-fg-1">{c.getValue() ?? "—"}</span> }),
];

export default function Market() {
  const [params, setParams] = useSearchParams();
  const watch = useQuery({ queryKey: ["watchlist"], queryFn: ({ signal }) => get("/api/market/watchlist", { signal }), refetchInterval: 15_000 });
  const live = useStream((s) => s.quotes);
  const rows = useMemo(
    () =>
      (watch.data ?? []).map((r) => {
        const q = live[r.instrument_key]; // the stream's conflated quote is fresher
        return q ? { ...r, ltp: q.ltp, prev_close: q.prev_close, change_pct: q.change_pct } : r;
      }),
    [watch.data, live],
  );
  const symbol = params.get("symbol") ?? rows[0]?.symbol ?? "";
  const bars = useQuery({
    queryKey: keys.bars(symbol, 250),
    queryFn: ({ signal }) => get("/api/market/{symbol}/bars", { params: { symbol }, query: { days: 250 }, signal }),
    enabled: !!symbol,
  });
  const events = useQuery({
    queryKey: keys.typed({ symbol }),
    queryFn: ({ signal }) => get("/api/events/typed", { query: { symbol: symbol || undefined, limit: 100 }, signal }),
  });
  const candles = useMemo(() => (bars.data?.bars ?? []).map((b) => ({ time: b.date, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume })), [bars.data]);
  const markers = useMemo(() => (bars.data?.markers ?? []).map((m) => ({ time: m.date, kind: m.kind, text: m.text })), [bars.data]);

  return (
    <div className="grid grid-cols-1 content-start gap-2 p-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <Panel title={`Universe · ${rows.length}`} className="min-h-[420px]">
        <DataTable
          label="Watchlist"
          data={rows}
          columns={WATCH_COLUMNS}
          loading={watch.isLoading}
          error={watch.error}
          getRowId={(r) => r.instrument_key}
          onRowClick={(r) => setParams({ symbol: r.symbol })}
          rowLabel={(r) => `Chart ${r.symbol}`}
          empty="No quotes yet. The universe appears with the first poll of a session (or the newest tape)."
        />
      </Panel>
      <div className="flex flex-col gap-2">
        <Panel
          title={symbol ? `${symbol} · daily · ₹ (${bars.data?.adjusted ? "adjusted" : "raw"})` : "Chart"}
          actions={<span className="text-2xs text-fg-2">{bars.data ? `source: ${bars.data.source}` : ""} · ▲ entry · ▼ exit · ● announcement</span>}
        >
          {candles.length ? (
            <Chart label={`${symbol} daily candles in rupees with volume, entries, exits and announcements`} candles={candles} markers={markers} height={340} />
          ) : (
            <Empty>{bars.isLoading ? "Loading…" : symbol ? `No daily history for ${symbol} (none taped yet).` : "Pick a symbol."}</Empty>
          )}
        </Panel>
        <Panel title={`Announcements${symbol ? ` · ${symbol}` : ""}`}>
          {!events.data?.length ? (
            <Empty>No classified announcements{symbol ? ` for ${symbol}` : ""}.</Empty>
          ) : (
            <ul className="text-xs">
              {events.data.map((e) => {
                const d = e.data as { title?: string; url?: string | null; model?: string };
                return (
                  <li key={e.event_id} className="flex items-center gap-2 border-b px-2 py-1">
                    <span className="w-24 shrink-0 text-fg-2">
                      <DateIST value={e.published_at} /> <TimeIST value={e.published_at} />
                    </span>
                    <span className="w-16 shrink-0">{e.symbol}</span>
                    {e.announcement_type && <Badge>{e.announcement_type}</Badge>}
                    {e.direction && <Badge tone={e.direction === "negative" ? "warn" : "neutral"}>{e.direction}</Badge>}
                    {e.materiality && <Badge>{e.materiality}</Badge>}
                    {d.url ? (
                      <a href={d.url} target="_blank" rel="noreferrer noopener" className="truncate text-accent hover:underline">
                        {d.title}
                      </a>
                    ) : (
                      <span className="truncate text-fg-0">{d.title}</span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
