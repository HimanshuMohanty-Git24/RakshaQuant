// 5. AI Desk (§6.5): which model does each role, how healthy and fast it is, what it costs,
// the decision models and their calibration, and whether the advisors earn their keep.

import { useQuery } from "@tanstack/react-query";
import { createColumnHelper } from "@tanstack/react-table";
import { useMemo } from "react";

import { get, type Schemas } from "../api/client";
import { keys, useConfig } from "../api/queries";
import { num, pct } from "../lib/format";
import { Badge } from "../ui/Badge";
import { Chart } from "../ui/Chart";
import { DataTable, type Columns } from "../ui/DataTable";
import { Money, TimeIST } from "../ui/Num";
import { Empty, Panel } from "../ui/Panel";
import { useSessionDay } from "./common";

type Call = Schemas["LLMCallRow"];
type Spend = Schemas["SpendRow"];

const c = createColumnHelper<Call>();
const CALL_COLUMNS: Columns<Call> = [
  c.accessor("ts", { header: "Time (IST)", size: 84, cell: (x) => <TimeIST value={x.getValue()} /> }),
  c.accessor("role", { header: "Role", size: 70 }),
  c.accessor("book_id", { header: "Book", size: 50, cell: (x) => x.getValue() ?? "—" }),
  c.accessor((r) => `${r.provider}:${r.model}`, { id: "model", header: "Model", size: 220, cell: (x) => <span className="font-mono">{x.getValue()}</span> }),
  c.accessor("outcome", { header: "Outcome", size: 110, cell: (x) => <Badge tone={x.getValue() === "ok" ? "neutral" : "warn"}>{x.getValue()}</Badge> }),
  c.accessor("tokens_in", { header: "In", size: 64, meta: { align: "right" }, cell: (x) => num(x.getValue(), 0) }),
  c.accessor("tokens_out", { header: "Out", size: 64, meta: { align: "right" }, cell: (x) => num(x.getValue(), 0) }),
  c.accessor("latency_ms", { header: "Latency", size: 80, meta: { align: "right" }, cell: (x) => `${num(x.getValue(), 0)} ms` }),
  c.accessor("cost_inr", { header: "Cost", size: 80, meta: { align: "right" }, cell: (x) => <Money value={x.getValue()} /> }),
  c.accessor("cache_hit", { header: "Cache", size: 56, cell: (x) => (x.getValue() ? "hit" : "—") }),
  c.accessor("prompt_version", { header: "Prompt", size: 170, cell: (x) => <span className="font-mono text-fg-1">{x.getValue()}</span> }),
];

function SpendBars({ rows }: { rows: Spend[] }) {
  if (!rows.length) return <Empty>No AI spend in this period.</Empty>;
  const max = Math.max(...rows.map((r) => Number(r.cost_inr)), 0.0001);
  return (
    <ul className="flex flex-col gap-1 p-2 text-xs">
      {rows.map((r) => (
        <li key={r.key} className="grid grid-cols-[minmax(0,180px)_1fr_auto] items-center gap-2">
          <span className="truncate font-mono text-fg-1">{r.key}</span>
          <span className="h-2 rounded-sm bg-accent" style={{ width: `${(Number(r.cost_inr) / max) * 100}%` }} aria-hidden="true" />
          <span className="num text-right text-fg-0">
            <Money value={r.cost_inr} /> <span className="text-fg-2">· {num(r.calls, 0)} calls</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

export default function AIDesk() {
  const day = useSessionDay();
  const monthStart = `${day.slice(0, 8)}01`;
  const { data: config } = useConfig();
  const models = useQuery({ queryKey: keys.aiModels, queryFn: ({ signal }) => get("/api/ai/models", { signal }), refetchInterval: 30_000 });
  const calls = useQuery({ queryKey: keys.aiCalls({ limit: 500 }), queryFn: ({ signal }) => get("/api/ai/calls", { query: { limit: 500 }, signal }) });
  const today = useQuery({ queryKey: keys.aiSpend({ group_by: "role", since: day }), queryFn: ({ signal }) => get("/api/ai/spend", { query: { group_by: "role", since: day, until: day }, signal }) });
  const month = useQuery({ queryKey: keys.aiSpend({ group_by: "model", since: monthStart }), queryFn: ({ signal }) => get("/api/ai/spend", { query: { group_by: "model", since: monthStart }, signal }) });
  const dms = useQuery({ queryKey: keys.decisionModels({}), queryFn: ({ signal }) => get("/api/ai/decision-models", { signal }) });
  const calibration = useQuery({ queryKey: ["ai", "calibration"], queryFn: ({ signal }) => get("/api/ai/calibration", { signal }) });
  const books = useQuery({ queryKey: keys.books, queryFn: ({ signal }) => get("/api/books", { signal }) });
  const reports = useQuery({ queryKey: ["reports"], queryFn: ({ signal }) => get("/api/reports", { query: { limit: 90 }, signal }) });

  const netLines = useMemo(() => {
    const days = [...(reports.data ?? [])].reverse();
    const ids = [...new Set(days.flatMap((d) => Object.keys(d.books)))].filter((b) => days.some((d) => d.books[b]?.net_ai_value_inr != null));
    return ids.slice(0, 3).map((b, i) => ({
      name: `${b} − baseline`,
      tone: (["accent", "warn", "neutral"] as const)[i]!,
      points: days.filter((d) => d.books[b]?.net_ai_value_inr != null).map((d) => ({ time: d.date, value: d.books[b]!.net_ai_value_inr! })),
    }));
  }, [reports.data]);
  const dm = config?.decision_models as { laya_enabled?: boolean; laya_checkpoint?: string; jev_configured?: boolean } | undefined;

  return (
    <div className="grid grid-cols-1 content-start gap-2 p-2 xl:grid-cols-2">
      <Panel title="Roles and models" className="xl:col-span-2">
        {!models.data?.length ? (
          <Empty>{models.isLoading ? "Loading…" : "No LLM roles."}</Empty>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line-strong text-left text-2xs text-fg-2">
                {["Role", "Model (primary first)", "Calls today", "Errors", "p50", "p95", "Last outcome", "Last call (IST)"].map((h) => (
                  <th key={h} className="px-2 py-0.5 font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {models.data.flatMap((role) =>
                role.enabled
                  ? role.models.map((m, i) => (
                      <tr key={`${role.role}${m.model}`} className="border-b">
                        <td className="px-2 py-0.5">{i === 0 ? role.role : ""}</td>
                        <td className="px-2 py-0.5 font-mono">{i === 0 ? m.model : `fallback ${m.model}`}</td>
                        <td className="num px-2 py-0.5 text-right">{num(m.calls_today, 0)}</td>
                        <td className="num px-2 py-0.5 text-right">{num(m.errors_today, 0)}</td>
                        <td className="num px-2 py-0.5 text-right">{m.p50_latency_ms == null ? "—" : `${num(m.p50_latency_ms, 0)} ms`}</td>
                        <td className="num px-2 py-0.5 text-right">{m.p95_latency_ms == null ? "—" : `${num(m.p95_latency_ms, 0)} ms`}</td>
                        <td className="px-2 py-0.5">{m.last_outcome ? <Badge tone={m.last_outcome === "ok" ? "neutral" : "warn"}>{m.last_outcome}</Badge> : "—"}</td>
                        <td className="px-2 py-0.5">{m.last_call ? <TimeIST value={m.last_call} /> : "—"}</td>
                      </tr>
                    ))
                  : [
                      <tr key={role.role} className="border-b text-fg-2">
                        <td className="px-2 py-0.5">{role.role}</td>
                        <td className="px-2 py-0.5" colSpan={7}>
                          not configured (LLM_ROLE_{role.role.toUpperCase()})
                        </td>
                      </tr>,
                    ],
              )}
            </tbody>
          </table>
        )}
      </Panel>
      <Panel title="Spend today, by role">
        <SpendBars rows={today.data?.rows ?? []} />
      </Panel>
      <Panel title="Spend this month, by provider:model">
        <SpendBars rows={month.data?.rows ?? []} />
      </Panel>
      <Panel title="Decision models">
        <div className="flex flex-wrap gap-4 px-2 py-1 text-xs text-fg-1">
          <span>Laya: <span className="text-fg-0">{dm?.laya_enabled ? `on (${dm.laya_checkpoint ?? "—"})` : "off"}</span></span>
          <span>Jev: <span className="text-fg-0">{dm?.jev_configured ? "configured" : "not configured"}</span></span>
        </div>
        {dms.data?.length ? (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line-strong text-left text-2xs text-fg-2">
                {["Task", "Model", "Calls", "p50", "p95", "Escalated", "Calibrated", "Shadow"].map((h) => (
                  <th key={h} className="px-2 py-0.5 font-normal">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dms.data.map((d) => (
                <tr key={`${d.task}${d.model}${d.checkpoint}`} className="border-b">
                  <td className="px-2 py-0.5">{d.task}</td>
                  <td className="px-2 py-0.5 font-mono">{d.model}:{d.checkpoint}</td>
                  <td className="num px-2 py-0.5 text-right">{num(d.calls, 0)}</td>
                  <td className="num px-2 py-0.5 text-right">{d.p50_latency_ms == null ? "—" : `${num(d.p50_latency_ms, 0)} ms`}</td>
                  <td className="num px-2 py-0.5 text-right">{d.p95_latency_ms == null ? "—" : `${num(d.p95_latency_ms, 0)} ms`}</td>
                  <td className="num px-2 py-0.5 text-right">{d.escalation_rate == null ? "—" : pct(d.escalation_rate * 100)}</td>
                  <td className="num px-2 py-0.5 text-right">{d.calibrated_rate == null ? "—" : pct(d.calibrated_rate * 100)}</td>
                  <td className="num px-2 py-0.5 text-right">{num(d.shadow_calls, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No decision-model calls recorded.</Empty>
        )}
      </Panel>
      <Panel title="Calibration">
        {calibration.data?.fitted ? (
          <table className="w-full text-xs">
            <tbody>
              {Object.entries(calibration.data.temperatures).map(([k, t]) => (
                <tr key={k} className="border-b">
                  <td className="px-2 py-0.5 font-mono text-fg-1">{k}</td>
                  <td className="num px-2 py-0.5 text-right">T = {num(t, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>Uncalibrated: the reliability diagram and Brier scores need a labelled set, which the owner skipped for now (decision OD-11). Questions Laya is unsure of go to Jev when its key is set; otherwise the typed veto abstains.</Empty>
        )}
      </Panel>
      <Panel title="Veto precision (vetoed signals whose counterfactual lost)">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-line-strong text-left text-2xs text-fg-2">
              {["Book", "Advisor", "Vetoes", "Settled", "Correct", "Precision", "95% CI", "Loss avoided", "Gain forgone"].map((h) => (
                <th key={h} className="px-2 py-0.5 font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(books.data?.books ?? []).filter((b) => b.advisor !== "none").map((b) => {
              const v = b.veto_precision;
              return (
                <tr key={b.book_id} className="border-b">
                  <td className="px-2 py-0.5">{b.book_id}</td>
                  <td className="px-2 py-0.5 text-fg-1">{b.advisor}</td>
                  <td className="num px-2 py-0.5 text-right">{num(v.vetoes, 0)}</td>
                  <td className="num px-2 py-0.5 text-right">{num(v.settled, 0)}</td>
                  <td className="num px-2 py-0.5 text-right">{num(v.correct, 0)}</td>
                  <td className="num px-2 py-0.5 text-right">{v.precision == null ? "—" : pct(v.precision * 100)}</td>
                  <td className="num px-2 py-0.5 text-right">{v.ci95 ? `${pct(v.ci95[0] * 100)}–${pct(v.ci95[1] * 100)}` : "—"}</td>
                  <td className="px-2 py-0.5 text-right"><Money value={v.loss_avoided_inr} /></td>
                  <td className="px-2 py-0.5 text-right"><Money value={v.gain_forgone_inr} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="px-2 py-1 text-2xs text-fg-2">Pre-registered rule: keep an advisor only if its net AI value is positive and the CI's lower bound is above 50% with at least 30 settled vetoes.</p>
      </Panel>
      <Panel title="Net AI value vs book A (₹, after AI spend, by day)">
        {netLines.some((l) => l.points.length) ? (
          <Chart label="Net AI value of each advised book against the baseline, rupees, by session" lines={netLines} height={200} axis="inr" />
        ) : (
          <Empty>
            Daily reports carry this series (written at 15:45 IST). Now:{" "}
            {(books.data?.books ?? []).filter((b) => b.vs).map((b) => (
              <span key={b.book_id} className="mr-3">
                {b.book_id} <Money value={b.net_ai_value_inr} pnl />
              </span>
            ))}
          </Empty>
        )}
      </Panel>
      <Panel title="Calls" className="xl:col-span-2 h-[360px]">
        <DataTable label="LLM calls" data={calls.data} columns={CALL_COLUMNS} loading={calls.isLoading} error={calls.error} getRowId={(r) => `${r.seq}`} empty="No LLM calls recorded (no role is configured, or none was needed)." />
      </Panel>
    </div>
  );
}
