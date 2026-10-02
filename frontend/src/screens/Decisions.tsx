// Decisions: what every book did with every signal, filterable; each row opens its lineage.

import { useQuery } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { useState } from "react";
import { useNavigate } from "react-router";

import { get, type Schemas } from "../api/client";
import { keys } from "../api/queries";
import { Badge } from "../ui/Badge";
import { DataTable, downloadCsv, type Columns } from "../ui/DataTable";
import { TimeIST } from "../ui/Num";
import { Panel, ToolButton } from "../ui/Panel";
import { DateFilter, DecisionLink, DISPOSITION, Select, SymbolLink, TextFilter, useBookIds, useSessionDay } from "./common";

type Row = Schemas["DecisionRow"];

const col = createColumnHelper<Row>();
export const DECISION_COLUMNS: Columns<Row> = [
  col.accessor("ts", { header: "Time (IST)", size: 84, cell: (c) => <TimeIST value={c.getValue()} /> }),
  col.accessor("book_id", { header: "Book", size: 52 }),
  col.accessor("symbol", { header: "Symbol", size: 96, cell: (c) => <SymbolLink symbol={c.getValue()} /> }),
  col.accessor("strategy", { header: "Strategy", size: 120 }),
  col.accessor("disposition", {
    header: "Outcome",
    size: 100,
    cell: (c) => <Badge tone={c.getValue() === "submitted" ? "neutral" : undefined}>{DISPOSITION[c.getValue()] ?? c.getValue()}</Badge>,
  }),
  col.accessor("detail", { header: "Detail", size: 360, cell: (c) => <span className="text-fg-1">{c.getValue() || "—"}</span> }),
  col.accessor("decision_id", { header: "Decision", size: 110, cell: (c) => <DecisionLink id={c.getValue()} /> }),
];

const OUTCOMES = ["", "submitted", "vetoed", "risk_rejected", "broker_rejected", "regime_gated", "policy_skipped", "shadow_strategy", "unknown"];

export default function Decisions() {
  const day = useSessionDay();
  const books = useBookIds();
  const navigate = useNavigate();
  const [book, setBook] = useState("");
  const [symbol, setSymbol] = useState("");
  const [strategy, setStrategy] = useState("");
  const [outcome, setOutcome] = useState("");
  const [date, setDate] = useState<string | null>(null);
  const query = {
    book: book || undefined,
    symbol: symbol.trim().toUpperCase() || undefined,
    strategy: strategy.trim() || undefined,
    outcome: outcome || undefined,
    date: date ?? day,
    limit: 1000,
  };
  const rows = useQuery({ queryKey: keys.decisions(query), queryFn: ({ signal }) => get("/api/decisions", { query, signal }) });
  return (
    <div className="flex h-full flex-col gap-2 p-2">
      <div className="flex flex-wrap items-end gap-3">
        <Select label="Book" value={book} onChange={setBook} options={[{ value: "", label: "All" }, ...books.map((b) => ({ value: b, label: b }))]} />
        <TextFilter label="Symbol" value={symbol} onChange={setSymbol} placeholder="e.g. INFY" />
        <TextFilter label="Strategy" value={strategy} onChange={setStrategy} placeholder="e.g. momentum" width="w-32" />
        <Select label="Outcome" value={outcome} onChange={setOutcome} options={OUTCOMES.map((o) => ({ value: o, label: o ? (DISPOSITION[o] ?? o) : "All" }))} />
        <DateFilter value={date ?? day} onChange={setDate} />
      </div>
      <Panel
        className="min-h-0 flex-1"
        title={`Decisions · ${rows.data?.length ?? 0}`}
        actions={<ToolButton onClick={() => downloadCsv(`decisions-${query.date}`, rows.data ?? [], DECISION_COLUMNS)} disabled={!rows.data?.length}>CSV</ToolButton>}
      >
        <DataTable
          label="Decisions"
          data={rows.data}
          columns={DECISION_COLUMNS}
          loading={rows.isLoading}
          error={rows.error}
          getRowId={(r) => `${r.seq}`}
          onRowClick={(r) => navigate(`/decisions/${r.decision_id}`)}
          rowLabel={(r) => `${r.book_id} ${r.symbol} ${r.strategy}: open the decision`}
          empty="No decisions match. Decisions run once a session, in the entry window."
        />
      </Panel>
    </div>
  );
}
