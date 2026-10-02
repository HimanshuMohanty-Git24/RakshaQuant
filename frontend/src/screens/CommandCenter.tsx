// 1. Command Center (§6.5): the books, the risk strip, positions, today's decisions, alerts and
// the session strip. Every number is a server projection; rows open the Decision Inspector.

import { useQuery } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { Group, Panel as Pane, Separator } from "react-resizable-panels";
import { useNavigate } from "react-router";

import { get, type Schemas } from "../api/client";
import { keys, useSummary } from "../api/queries";
import { cx } from "../lib/cx";
import { hhmmIST, inrCompact, num, ratio } from "../lib/format";
import { Badge } from "../ui/Badge";
import { DataTable, type Columns } from "../ui/DataTable";
import { Meter } from "../ui/Meter";
import { Money, Pct, Price, Qty, TimeIST } from "../ui/Num";
import { Empty, Panel } from "../ui/Panel";
import { BookPicker, DISPOSITION, SymbolLink, useSessionDay } from "./common";

type Book = Schemas["BookSummary"];
type Comparison = Schemas["BookComparison"];
type Position = Schemas["PositionRow"];
type Usage = Schemas["Utilisation"];
type DecisionRow = Schemas["DecisionRow"];

function Gutter({ vertical = false }: { vertical?: boolean }) {
  return (
    <Separator
      className={cx(
        "group flex items-center justify-center outline-none",
        vertical ? "h-2" : "w-2",
      )}
    >
      <span className={cx("bg-transparent group-hover:bg-accent group-focus-visible:bg-accent", vertical ? "h-px w-full" : "h-full w-px")} />
    </Separator>
  );
}

// -- books -----------------------------------------------------------------------------------

interface BookLine extends Book {
  comparison?: Comparison;
}

const bookCol = createColumnHelper<BookLine>();
const BOOK_COLUMNS: Columns<BookLine> = [
  bookCol.accessor("book_id", { header: "Book", size: 52, cell: (c) => <span className="font-medium">{c.getValue()}</span> }),
  bookCol.accessor("advisor", { header: "Advisor", size: 84, cell: (c) => <span className="text-fg-1">{c.getValue()}</span> }),
  bookCol.accessor("equity", { header: "Equity", size: 116, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} /> }),
  bookCol.accessor("day_pnl", { header: "Day P&L", size: 104, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  bookCol.accessor("day_return_pct", { header: "Day %", size: 64, meta: { align: "right" }, cell: (c) => <Pct value={c.getValue()} pnl /> }),
  bookCol.accessor((r) => r.comparison?.return_pct ?? null, { id: "return", header: "Return", size: 66, meta: { align: "right" }, cell: (c) => <Pct value={c.getValue()} pnl /> }),
  bookCol.accessor("realized_pnl_today", { header: "Realised today", size: 106, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  bookCol.accessor("trades_today", { header: "Trades", size: 52, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  bookCol.accessor((r) => r.comparison?.net_ai_value_inr ?? null, { id: "net_ai", header: "Net AI value", size: 104, meta: { align: "right" }, cell: (c) => (c.getValue() === null ? <span className="text-fg-2">baseline</span> : <Money value={c.getValue()} pnl />) }),
  bookCol.accessor("kill_switch", { header: "Kill", size: 76, cell: (c) => <Badge tone={c.getValue() === "ARMED" ? "neutral" : undefined}>{c.getValue() === "HALT_NEW" ? "HALTED" : c.getValue()}</Badge> }),
];

function BooksPanel() {
  const { data: summary } = useSummary();
  const books = useQuery({ queryKey: keys.books, queryFn: ({ signal }) => get("/api/books", { signal }), refetchInterval: 15_000 });
  const rows = useMemo<BookLine[]>(
    () =>
      (summary?.books ?? []).map((b) => ({ ...b, comparison: books.data?.books.find((c) => c.book_id === b.book_id) })),
    [summary, books.data],
  );
  const valuation = summary?.books[0]?.valuation;
  return (
    <Panel
      className="h-full"
      title="Books"
      actions={
        <span className="text-2xs text-fg-2">
          {valuation === "live" ? "valued live" : valuation === "last_mark" ? "last mark" : "no valuation yet"}
        </span>
      }
    >
      <DataTable label="Books" data={rows} columns={BOOK_COLUMNS} getRowId={(r) => r.book_id} empty="No books configured." />
    </Panel>
  );
}

// -- risk strip --------------------------------------------------------------------------------

export function usageDetail(u: Usage): string {
  if (u.unit === "inr") return `${inrCompact(u.used)} / ${inrCompact(u.limit)}`;
  if (u.unit === "ratio") return `${ratio(u.used)} / ${ratio(u.limit)}`;
  return `${num(u.used, 0)} / ${num(u.limit, 0)}`;
}

function RiskStrip() {
  const [book, setBook] = useState<string | undefined>("A");
  const risk = useQuery({
    queryKey: keys.risk(book),
    queryFn: ({ signal }) => get("/api/risk", { query: { book }, signal }),
    refetchInterval: 10_000,
  });
  const view = risk.data?.books[0];
  const global = view?.kill_switches.find((k) => k.scope === "global");
  return (
    <Panel className="h-full" title="Risk" actions={<BookPicker value={book} onChange={setBook} />}>
      {risk.error ? (
        <Empty className="text-warn">Could not load the risk view.</Empty>
      ) : (
        <div className="flex flex-col gap-1.5 p-2">
          {view?.utilisation.map((u) => <Meter key={u.key} label={u.label} fraction={u.fraction} detail={usageDetail(u)} />)}
          <div className="mt-1 flex items-center gap-2 text-xs text-fg-1">
            Kill switch
            <Badge tone={!global || global.state === "ARMED" ? "neutral" : undefined}>
              {global ? (global.state === "HALT_NEW" ? "HALTED" : global.state) : "ARMED"}
            </Badge>
            {global && global.state !== "ARMED" && <span className="truncate text-fg-2">{global.reason}</span>}
            <span className="ml-auto text-2xs text-fg-2">{view?.valuation === "live" ? "live" : "from the last mark"}</span>
          </div>
        </div>
      )}
    </Panel>
  );
}

// -- positions ---------------------------------------------------------------------------------

const posCol = createColumnHelper<Position>();
const POSITION_COLUMNS: Columns<Position> = [
  posCol.accessor("book_id", { header: "Book", size: 50 }),
  posCol.accessor("symbol", { header: "Symbol", size: 96, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  posCol.accessor("quantity", { header: "Qty", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  posCol.accessor("avg_price", { header: "Avg", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  posCol.accessor("mark", { header: "LTP", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  posCol.accessor("unrealized_pnl", { header: "Unrl P&L", size: 104, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  posCol.accessor("stop_price", { header: "Stop", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  posCol.accessor("target_price", { header: "Target", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  posCol.accessor("held_sessions", { header: "Days", size: 50, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  posCol.accessor("strategy", { header: "Strategy", size: 110, cell: (c) => <span className="text-fg-1">{c.getValue() ?? "—"}</span> }),
];

function PositionsPanel() {
  const [book, setBook] = useState<string | undefined>(undefined);
  const navigate = useNavigate();
  const { data: summary } = useSummary();
  const positions = useQuery({
    queryKey: keys.positions(book),
    queryFn: ({ signal }) => get("/api/positions", { query: { book }, signal }),
    refetchInterval: 10_000,
  });
  return (
    <Panel className="h-full" title="Positions" actions={<BookPicker value={book} onChange={setBook} all />}>
      <DataTable
        label="Open positions"
        data={positions.data}
        columns={POSITION_COLUMNS}
        error={positions.error}
        loading={positions.isLoading}
        getRowId={(r) => `${r.book_id}|${r.instrument_key}|${r.product}`}
        onRowClick={(r) => r.entry_decision_id && navigate(`/decisions/${r.entry_decision_id}`)}
        rowLabel={(r) => `${r.book_id} ${r.symbol}: open the entry decision`}
        empty={summary?.market_open ? "No open positions." : "No open positions. Market closed."}
      />
    </Panel>
  );
}

// -- today's decisions ----------------------------------------------------------------------------

interface SignalLine {
  signal_id: string;
  decision_id: string;
  ts: string;
  symbol: string;
  strategy: string;
  books: DecisionRow[];
}

function group(rows: DecisionRow[]): SignalLine[] {
  const lines = new Map<string, SignalLine>();
  for (const r of rows) {
    if (r.disposition === "shadow_strategy") continue;
    const line = lines.get(r.signal_id) ?? { signal_id: r.signal_id, decision_id: r.decision_id, ts: r.ts, symbol: r.symbol, strategy: r.strategy, books: [] };
    line.books.push(r);
    lines.set(r.signal_id, line);
  }
  return [...lines.values()].map((l) => ({ ...l, books: [...l.books].sort((a, b) => a.book_id.localeCompare(b.book_id)) }));
}

function TodaysDecisions() {
  const day = useSessionDay();
  const navigate = useNavigate();
  const query = { date: day, limit: 1000 };
  const decisions = useQuery({
    queryKey: keys.decisions(query),
    queryFn: ({ signal }) => get("/api/decisions", { query, signal }),
    refetchInterval: 15_000,
  });
  const lines = useMemo(() => group(decisions.data ?? []), [decisions.data]);
  return (
    <Panel className="h-full" title="Today's decisions" actions={<span className="text-2xs text-fg-2">{lines.length} signals</span>}>
      {lines.length === 0 ? (
        <Empty>{decisions.isLoading ? "Loading…" : "No signals today. Decisions run once, in the entry window."}</Empty>
      ) : (
        <ul className="text-xs">
          {lines.map((l) => (
            <li key={l.signal_id}>
              <button
                type="button"
                onClick={() => navigate(`/decisions/${l.decision_id}`)}
                className="flex h-6 w-full items-center gap-2 border-b px-2 text-left hover:bg-bg-2"
              >
                <span className="num w-10 text-fg-2">{hhmmIST(l.ts)}</span>
                <span className="w-20 truncate text-fg-0">{l.symbol}</span>
                <span className="w-28 truncate text-fg-1">{l.strategy}</span>
                <span className="flex flex-1 gap-1">
                  {l.books.map((b) => (
                    <Badge key={b.book_id} tone={b.disposition === "submitted" ? "neutral" : undefined} title={b.detail || b.disposition}>
                      {`${b.book_id} ${DISPOSITION[b.disposition] ?? b.disposition}`}
                    </Badge>
                  ))}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// -- alerts ------------------------------------------------------------------------------------

function AlertsPanel() {
  const alerts = useQuery({
    queryKey: ["alerts", { limit: 100 }],
    queryFn: ({ signal }) => get("/api/alerts", { query: { limit: 100 }, signal }),
    refetchInterval: 15_000,
  });
  return (
    <Panel className="h-full" title="Alerts">
      {(alerts.data ?? []).length === 0 ? (
        <Empty>{alerts.isLoading ? "Loading…" : "No alerts."}</Empty>
      ) : (
        <ul className="text-xs">
          {alerts.data!.map((a) => (
            <li key={a.seq} className="flex h-6 items-center gap-2 border-b px-2">
              <TimeIST value={a.ts} className="w-14 text-fg-2" />
              <Badge tone={a.level === "INFO" ? "neutral" : a.level === "CRITICAL" ? "crit" : "warn"}>{a.level}</Badge>
              <span className="w-40 truncate font-mono text-2xs text-fg-1">{a.key}</span>
              <span className="truncate text-fg-0" title={a.message}>
                {a.message}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// -- session strip ---------------------------------------------------------------------------------

const LABEL: Record<string, string> = { OPEN: "Open", ENTRY_WINDOW: "Entry", MONITOR: "Monitor", CLOSE: "Close", REPORT: "Report", EXIT: "Exit" };

function SessionStrip() {
  const { data: summary } = useSummary();
  const current = summary?.session?.state;
  const steps = summary?.schedule ?? [];
  if (!steps.length) {
    return <div className="text-xs text-fg-2">No session today (holiday or weekend).</div>;
  }
  const entry = steps.findIndex((s) => s.state === "ENTRY_WINDOW");
  return (
    <ol aria-label="Session" className="flex flex-wrap items-center gap-1 text-xs">
      <li className={cx("rounded-sm border px-1.5", current === "PRE_OPEN" ? "border-accent text-fg-0" : "border-line text-fg-2")}>Pre-open</li>
      {steps.map((s, i) => (
        <li key={s.state} className="flex items-center gap-1">
          <span aria-hidden="true" className="text-fg-2">
            ▸
          </span>
          <span
            aria-current={current === s.state ? "step" : undefined}
            className={cx("rounded-sm border px-1.5", current === s.state ? "border-accent text-fg-0" : "border-line text-fg-2")}
          >
            {LABEL[s.state] ?? s.state} <span className="num">{hhmmIST(s.at)}</span>
            {i === entry && steps[i + 1] && <span className="num">–{hhmmIST(steps[i + 1]!.at)}</span>}
          </span>
        </li>
      ))}
      <li className="ml-2 text-2xs text-fg-2">IST</li>
    </ol>
  );
}

export default function CommandCenter() {
  return (
    <div className="flex h-full min-h-[640px] flex-col gap-2 p-2">
      <SessionStrip />
      <Group orientation="horizontal" className="min-h-0 flex-1">
        <Pane defaultSize="60%" minSize="35%">
          <Group orientation="vertical">
            <Pane defaultSize="34%" minSize="20%">
              <BooksPanel />
            </Pane>
            <Gutter vertical />
            <Pane minSize="20%">
              <PositionsPanel />
            </Pane>
          </Group>
        </Pane>
        <Gutter />
        <Pane minSize="25%">
          <Group orientation="vertical">
            <Pane defaultSize="36%" minSize="20%">
              <RiskStrip />
            </Pane>
            <Gutter vertical />
            <Pane defaultSize="34%" minSize="15%">
              <TodaysDecisions />
            </Pane>
            <Gutter vertical />
            <Pane minSize="15%">
              <AlertsPanel />
            </Pane>
          </Group>
        </Pane>
      </Group>
    </div>
  );
}
