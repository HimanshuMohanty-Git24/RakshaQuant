// 8. System (§6.5): the engine's tasks and health, feed freshness, the store, reconciliation,
// the read-only configuration, a filterable log tail, and every version that shapes a decision.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { get } from "../api/client";
import { useConfig, useSystem } from "../api/queries";
import { duration, num } from "../lib/format";
import { Badge } from "../ui/Badge";
import { TimeIST } from "../ui/Num";
import { Empty, Panel } from "../ui/Panel";
import { Stat } from "../ui/Stat";
import { Select, TextFilter } from "./common";

function bytes(n: number): string {
  if (n >= 1 << 30) return `${num(n / (1 << 30), 2)} GB`;
  if (n >= 1 << 20) return `${num(n / (1 << 20), 1)} MB`;
  return `${num(n / 1024, 0)} KB`;
}

export default function System() {
  const { data: system } = useSystem();
  const { data: config } = useConfig();
  const [level, setLevel] = useState("");
  const [contains, setContains] = useState("");
  const query = { level: level || undefined, contains: contains.trim() || undefined, limit: 300 };
  const logs = useQuery({ queryKey: ["logs", query], queryFn: ({ signal }) => get("/api/logs", { query, signal }), refetchInterval: 10_000 });

  return (
    <div className="grid grid-cols-1 content-start gap-2 p-2 xl:grid-cols-2">
      <Panel title="Health">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3 p-3">
          <Stat label="Run" value={system?.running ? "running" : "idle"} />
          <Stat label="Uptime" value={system?.uptime_s == null ? "—" : duration(system.uptime_s)} />
          <Stat label="Loop lag" value={system?.loop_lag_ms == null ? "—" : `${num(system.loop_lag_ms, 0)} ms`} />
          <Stat label="Last heartbeat" value={system?.last_heartbeat ? <TimeIST value={system.last_heartbeat} /> : "—"} />
          <Stat label="Event store" value={system ? bytes(system.store_bytes) : "—"} delta={<span className="font-mono text-2xs text-fg-2">{system?.store_path} · seq {num(system?.last_seq ?? null, 0)}</span>} />
          <Stat label="Schema" value={system ? `v${system.schema_version}` : "—"} />
        </div>
        <div className="border-t px-3 py-2 text-xs">
          <span className="text-2xs text-fg-2">Tasks</span>
          <div className="mt-1 flex flex-wrap gap-1">
            {system?.tasks.length ? system.tasks.map((t) => <Badge key={t}>{t}</Badge>) : <span className="text-fg-2">No engine tasks (no session running).</span>}
          </div>
        </div>
        <div className="border-t px-3 py-2 text-xs">
          <span className="text-2xs text-fg-2">Feed freshness (age of the freshest quote, by source)</span>
          <div className="mt-1 flex flex-wrap gap-3">
            {Object.entries(system?.quote_age_s ?? {}).length
              ? Object.entries(system!.quote_age_s).map(([source, age]) => (
                  <span key={source}>
                    <span className="font-mono text-fg-1">{source}</span> <span className={age > 1800 ? "text-warn" : "text-fg-0"}>{duration(age)}</span>
                  </span>
                ))
              : <span className="text-fg-2">No quotes in memory.</span>}
          </div>
        </div>
      </Panel>
      <Panel title="Reconciliation (OMS vs broker)">
        {system?.last_reconcile.length ? (
          <table className="w-full text-xs">
            <tbody>
              {system.last_reconcile.map((r) => (
                <tr key={`${r.book_id}${r.scope}`} className="border-b">
                  <td className="px-2 py-0.5">{r.book_id}</td>
                  <td className="px-2 py-0.5 font-mono text-fg-1">{r.scope}</td>
                  <td className="px-2 py-0.5">{r.in_sync ? <Badge>in sync</Badge> : <Badge tone="warn">drift</Badge>}</td>
                  <td className="px-2 py-0.5"><TimeIST value={r.ts} label /></td>
                  <td className="truncate px-2 py-0.5 text-fg-1">{r.diffs.join("; ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>Not reconciled yet (each session reconciles pre-open and every 15 minutes).</Empty>
        )}
      </Panel>
      <Panel title="Versions">
        <dl className="grid grid-cols-[180px_minmax(0,1fr)] gap-x-3 px-3 py-2 text-xs">
          {Object.entries(system?.versions ?? {}).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-fg-2">{k}</dt>
              <dd className="truncate font-mono text-fg-0">{v}</dd>
            </div>
          ))}
          <dt className="text-fg-2">risk limits</dt>
          <dd className="font-mono text-fg-0">{config?.limits_hash ?? "—"}</dd>
          {Object.entries(system?.prompts ?? {}).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-fg-2">prompt {k}</dt>
              <dd className="truncate font-mono text-fg-0">{v}</dd>
            </div>
          ))}
          {Object.entries(config?.llm_roles ?? {}).map(([role, chain]) => (
            <div key={role} className="contents">
              <dt className="text-fg-2">role {role}</dt>
              <dd className="truncate font-mono text-fg-0">{chain.length ? chain.join(" → ") : <span className="text-fg-2">not configured</span>}</dd>
            </div>
          ))}
        </dl>
      </Panel>
      <Panel title="Configuration (read-only, redacted)">
        <pre className="whitespace-pre-wrap px-3 py-2 font-mono text-xs leading-5 text-fg-1">{config ? JSON.stringify(config, null, 2) : "—"}</pre>
      </Panel>
      <Panel
        title="Logs (newest first)"
        className="h-[420px] xl:col-span-2"
        actions={
          <div className="flex items-center gap-2">
            <Select label="Level" value={level} onChange={setLevel} options={["", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => ({ value: l, label: l || "All" }))} />
            <TextFilter label="Contains" value={contains} onChange={setContains} placeholder="e.g. reconcile" width="w-40" />
          </div>
        }
      >
        {!logs.data?.length ? (
          <Empty>{logs.isLoading ? "Loading…" : "No log lines match."}</Empty>
        ) : (
          <ul className="font-mono text-2xs leading-4">
            {logs.data.map((l, i) => (
              <li key={i} className="flex gap-2 border-b px-2 py-0.5">
                <TimeIST value={l.ts} className="shrink-0 text-fg-2" />
                <span className={`w-16 shrink-0 ${l.level === "WARNING" ? "text-warn" : l.level === "ERROR" || l.level === "CRITICAL" ? "text-down" : "text-fg-2"}`}>{l.level}</span>
                <span className="w-48 shrink-0 truncate text-fg-2">{l.logger}</span>
                <span className="min-w-0 break-words text-fg-0">{l.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
