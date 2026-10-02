// 3. Blotter (§6.5): orders, fills, positions, trades and rejections, with shared filters; every
// row opens its decision.

import { useQuery } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { useState } from "react";
import { useNavigate } from "react-router";

import { get, type Query, type Schemas } from "../api/client";
import { keys } from "../api/queries";
import { Badge } from "../ui/Badge";
import { DataTable, downloadCsv, type Columns } from "../ui/DataTable";
import { DateIST, Money, Price, Qty, TimeIST } from "../ui/Num";
import { Panel, ToolButton } from "../ui/Panel";
import { Tabs } from "../ui/Tabs";
import { DECISION_COLUMNS } from "./Decisions";
import { DateFilter, DecisionLink, Select, SymbolLink, TextFilter, useBookIds, useSessionDay } from "./common";

type Order = Schemas["OrderRow"];
type Fill = Schemas["FillRow"];
type Position = Schemas["PositionRow"];
type Trade = Schemas["TradeRow"];
type Decision = Schemas["DecisionRow"];

const o = createColumnHelper<Order>();
const ORDER_COLUMNS: Columns<Order> = [
  o.accessor("updated_ts", { header: "Updated (IST)", size: 96, cell: (c) => <TimeIST value={c.getValue()} /> }),
  o.accessor("book_id", { header: "Book", size: 50 }),
  o.accessor("symbol", { header: "Symbol", size: 90, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  o.accessor("side", { header: "Side", size: 56, cell: (c) => <Badge>{c.getValue()}</Badge> }),
  o.accessor("kind", { header: "Leg", size: 64 }),
  o.accessor("order_type", { header: "Type", size: 70 }),
  o.accessor("quantity", { header: "Qty", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  o.accessor("filled_qty", { header: "Filled", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  o.accessor("avg_fill_price", { header: "Avg fill", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  o.accessor("status", { header: "Status", size: 88, cell: (c) => <Badge>{c.getValue()}</Badge> }),
  o.accessor("reason", { header: "Reason", size: 80 }),
  o.accessor("strategy", { header: "Strategy", size: 110 }),
  o.accessor("client_order_id", { header: "Order", size: 130, cell: (c) => <span className="font-mono text-fg-1">{c.getValue()}</span> }),
  o.accessor("decision_id", { header: "Decision", size: 100, cell: (c) => <DecisionLink id={c.getValue()} /> }),
];

const f = createColumnHelper<Fill>();
const FILL_COLUMNS: Columns<Fill> = [
  f.accessor("ts", { header: "Time (IST)", size: 84, cell: (c) => <TimeIST value={c.getValue()} /> }),
  f.accessor("book_id", { header: "Book", size: 50 }),
  f.accessor("symbol", { header: "Symbol", size: 90, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  f.accessor("side", { header: "Side", size: 56, cell: (c) => <Badge>{c.getValue()}</Badge> }),
  f.accessor("quantity", { header: "Qty", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  f.accessor("price", { header: "Price", size: 90, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  f.accessor("charges", { header: "Charges", size: 90, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} /> }),
  f.accessor("client_order_id", { header: "Order", size: 130, cell: (c) => <span className="font-mono text-fg-1">{c.getValue()}</span> }),
  f.accessor("fill_id", { header: "Fill", size: 150, cell: (c) => <span className="font-mono text-fg-1">{c.getValue()}</span> }),
  f.accessor("decision_id", { header: "Decision", size: 100, cell: (c) => <DecisionLink id={c.getValue()} /> }),
];

const p = createColumnHelper<Position>();
const POSITION_COLUMNS: Columns<Position> = [
  p.accessor("book_id", { header: "Book", size: 50 }),
  p.accessor("symbol", { header: "Symbol", size: 90, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  p.accessor("product", { header: "Product", size: 64 }),
  p.accessor("quantity", { header: "Qty", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  p.accessor("avg_price", { header: "Avg", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  p.accessor("mark", { header: "LTP", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  p.accessor("unrealized_pnl", { header: "Unrl P&L", size: 104, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  p.accessor("realized_pnl", { header: "Realised", size: 100, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  p.accessor("stop_price", { header: "Stop", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  p.accessor("target_price", { header: "Target", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  p.accessor("entered_on", { header: "Entered", size: 92, cell: (c) => <DateIST value={c.getValue()} /> }),
  p.accessor("held_sessions", { header: "Days", size: 50, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  p.accessor("strategy", { header: "Strategy", size: 110 }),
];

const t = createColumnHelper<Trade>();
const TRADE_COLUMNS: Columns<Trade> = [
  t.accessor("exit_ts", { header: "Closed (IST)", size: 96, cell: (c) => <TimeIST value={c.getValue()} /> }),
  t.accessor("book_id", { header: "Book", size: 50 }),
  t.accessor("symbol", { header: "Symbol", size: 90, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  t.accessor("strategy", { header: "Strategy", size: 110 }),
  t.accessor("quantity", { header: "Qty", size: 64, meta: { align: "right" }, cell: (c) => <Qty value={c.getValue()} /> }),
  t.accessor("entry_price", { header: "Entry", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  t.accessor("exit_price", { header: "Exit", size: 86, meta: { align: "right" }, cell: (c) => <Price value={c.getValue()} /> }),
  t.accessor("exit_reason", { header: "Exit reason", size: 92, cell: (c) => <Badge>{c.getValue()}</Badge> }),
  t.accessor("gross_pnl", { header: "Gross", size: 100, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
  t.accessor("charges", { header: "Charges", size: 90, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} /> }),
  t.accessor("net_pnl", { header: "Net P&L", size: 104, meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl arrow /> }),
  t.accessor("decision_id", { header: "Entry decision", size: 110, cell: (c) => <DecisionLink id={c.getValue()} /> }),
  t.accessor("exit_decision_id", { header: "Exit decision", size: 110, cell: (c) => <DecisionLink id={c.getValue()} /> }),
];

const REJECTED = new Set(["vetoed", "risk_rejected", "broker_rejected"]);
const STATUSES = ["", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"];

function useRows<T>(name: string, path: Parameters<typeof get>[0], query: Query, enabled: boolean) {
  return useQuery({
    queryKey: [name, query],
    queryFn: ({ signal }) => get(path, { query, signal }) as Promise<T[]>,
    enabled,
  });
}

export default function Blotter() {
  const day = useSessionDay();
  const books = useBookIds();
  const navigate = useNavigate();
  const [tab, setTab] = useState("orders");
  const [book, setBook] = useState("");
  const [symbol, setSymbol] = useState("");
  const [strategy, setStrategy] = useState("");
  const [status, setStatus] = useState("");
  const [date, setDate] = useState<string | null>(null);
  const sym = symbol.trim().toUpperCase() || undefined;
  const base = { book: book || undefined, symbol: sym, date: date ?? day, limit: 1000 };

  const orders = useRows<Order>("orders", "/api/orders", { ...base, status: status || undefined }, tab === "orders");
  const fills = useRows<Fill>("fills", "/api/fills", base, tab === "fills");
  const trades = useRows<Trade>("trades", "/api/trades", { ...base, strategy: strategy.trim() || undefined }, tab === "trades");
  const positions = useQuery({
    queryKey: keys.positions(book || undefined),
    queryFn: ({ signal }) => get("/api/positions", { query: { book: book || undefined }, signal }),
    enabled: tab === "positions",
  });
  const decisions = useRows<Decision>("decisions", "/api/decisions", base, tab === "rejections");
  const rejections = (decisions.data ?? []).filter((d) => REJECTED.has(d.disposition));
  const shownPositions = (positions.data ?? []).filter((r) => !sym || r.symbol === sym);
  const open = (id: string | null | undefined) => id && navigate(`/decisions/${id}`);

  const csv = () => {
    const name = `${tab}-${base.date}`;
    if (tab === "orders") downloadCsv(name, orders.data ?? [], ORDER_COLUMNS);
    if (tab === "fills") downloadCsv(name, fills.data ?? [], FILL_COLUMNS);
    if (tab === "positions") downloadCsv(name, shownPositions, POSITION_COLUMNS);
    if (tab === "trades") downloadCsv(name, trades.data ?? [], TRADE_COLUMNS);
    if (tab === "rejections") downloadCsv(name, rejections, DECISION_COLUMNS);
  };

  return (
    <div className="flex h-full flex-col gap-2 p-2">
      <div className="flex flex-wrap items-end gap-3">
        <Select label="Book" value={book} onChange={setBook} options={[{ value: "", label: "All" }, ...books.map((b) => ({ value: b, label: b }))]} />
        <TextFilter label="Symbol" value={symbol} onChange={setSymbol} placeholder="e.g. INFY" />
        {tab === "trades" && <TextFilter label="Strategy" value={strategy} onChange={setStrategy} placeholder="e.g. momentum" width="w-32" />}
        {tab === "orders" && <Select label="Status" value={status} onChange={setStatus} options={STATUSES.map((s) => ({ value: s, label: s || "All" }))} />}
        {tab !== "positions" && <DateFilter value={date ?? day} onChange={setDate} />}
      </div>
      <Panel className="min-h-0 flex-1" title="Blotter" actions={<ToolButton onClick={csv}>CSV</ToolButton>} bodyClassName="flex flex-col">
        <Tabs
          label="Blotter"
          value={tab}
          onValueChange={setTab}
          tabs={[
            { value: "orders", label: "Orders", content: <DataTable label="Orders" data={orders.data} columns={ORDER_COLUMNS} loading={orders.isLoading} error={orders.error} getRowId={(r) => r.client_order_id} onRowClick={(r) => open(r.decision_id)} empty="No orders for these filters." /> },
            { value: "fills", label: "Fills", content: <DataTable label="Fills" data={fills.data} columns={FILL_COLUMNS} loading={fills.isLoading} error={fills.error} getRowId={(r) => r.fill_id} onRowClick={(r) => open(r.decision_id)} empty="No fills for these filters." /> },
            { value: "positions", label: "Positions", content: <DataTable label="Positions" data={shownPositions} columns={POSITION_COLUMNS} loading={positions.isLoading} error={positions.error} getRowId={(r) => `${r.book_id}|${r.instrument_key}|${r.product}`} onRowClick={(r) => open(r.entry_decision_id)} empty="No open positions." /> },
            { value: "trades", label: "Trades", content: <DataTable label="Trades" data={trades.data} columns={TRADE_COLUMNS} loading={trades.isLoading} error={trades.error} getRowId={(r) => r.trade_id} onRowClick={(r) => open(r.decision_id)} empty="No closed trades for these filters." /> },
            { value: "rejections", label: "Rejections", content: <DataTable label="Rejections" data={rejections} columns={DECISION_COLUMNS} loading={decisions.isLoading} error={decisions.error} getRowId={(r) => `${r.seq}`} onRowClick={(r) => open(r.decision_id)} empty="Nothing vetoed or rejected for these filters." /> },
          ]}
        />
      </Panel>
    </div>
  );
}
