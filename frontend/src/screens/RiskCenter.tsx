// 4. Risk Center (§6.5): limits as meters, the kill switches with HALT / RESUME / FLATTEN,
// sector exposure, equity against its peak, today's blocked checks, and event-calendar blocks.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { get, post, type Schemas } from "../api/client";
import { keys } from "../api/queries";
import { num } from "../lib/format";
import { useUi } from "../lib/ui";
import { Badge } from "../ui/Badge";
import { Chart } from "../ui/Chart";
import { ConfirmTyped } from "../ui/Dialog";
import { Meter } from "../ui/Meter";
import { DateIST, TimeIST } from "../ui/Num";
import { Empty, Panel, ToolButton } from "../ui/Panel";
import { useToasts } from "../ui/Toast";
import { usageDetail } from "./CommandCenter";
import { BookPicker } from "./common";

type BookRisk = Schemas["BookRisk"];

function Controls({ book }: { book: string }) {
  const [dialog, setDialog] = useState<"RESUME" | "FLATTEN" | null>(null);
  const setHalt = useUi((s) => s.setHalt);
  const push = useToasts((s) => s.push);
  const client = useQueryClient();
  const act = async (phrase: "RESUME" | "FLATTEN", reason: string) => {
    const result =
      phrase === "RESUME"
        ? await post("/api/risk/resume", { confirm: true, phrase, reason, book })
        : await post("/api/risk/flatten", { confirm: true, phrase, reason, book });
    push({ level: phrase === "FLATTEN" ? "crit" : "info", title: `${phrase} ${result.outcome.replace("_", " ")}`, body: result.detail });
    void client.invalidateQueries();
  };
  return (
    <div className="flex gap-1">
      <ToolButton tone="crit" onClick={() => setHalt(true)} title="Block new entries (all books)">
        HALT
      </ToolButton>
      <ToolButton onClick={() => setDialog("RESUME")}>RESUME {book}</ToolButton>
      <ToolButton tone="crit" onClick={() => setDialog("FLATTEN")}>FLATTEN {book}</ToolButton>
      <ConfirmTyped
        open={dialog !== null}
        onOpenChange={(open) => !open && setDialog(null)}
        title={dialog === "FLATTEN" ? `Flatten book ${book}` : `Resume book ${book}`}
        description={
          dialog === "FLATTEN"
            ? "Exits every position of this book through the OMS at the next risk tick, and blocks new entries."
            : "Re-arms this book's global kill switch: new entries are allowed again. A HALT file blocks this."
        }
        phrase={dialog ?? "RESUME"}
        confirmLabel={dialog === "FLATTEN" ? "Flatten" : "Resume"}
        onConfirm={(reason) => act(dialog ?? "RESUME", reason)}
      />
    </div>
  );
}

function Switches({ view }: { view: BookRisk }) {
  const rows = view.kill_switches.length
    ? view.kill_switches
    : [{ scope: "global", name: "global", state: "ARMED", reason: "never tripped", actor: "—", since: null }];
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="border-b border-line-strong text-left text-2xs text-fg-2">
          <th className="px-2 py-0.5 font-normal">Switch</th>
          <th className="px-2 py-0.5 font-normal">State</th>
          <th className="px-2 py-0.5 font-normal">Reason</th>
          <th className="px-2 py-0.5 font-normal">Actor</th>
          <th className="px-2 py-0.5 font-normal">Since (IST)</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((k) => (
          <tr key={`${k.scope}/${k.name}`} className="border-b">
            <td className="px-2 py-0.5 font-mono">{k.scope === k.name ? k.scope : `${k.scope}/${k.name}`}</td>
            <td className="px-2 py-0.5">
              <Badge tone={k.state === "ARMED" ? "neutral" : undefined}>{k.state === "HALT_NEW" ? "HALTED" : k.state}</Badge>
            </td>
            <td className="truncate px-2 py-0.5 text-fg-1">{k.reason}</td>
            <td className="px-2 py-0.5 text-fg-1">{k.actor}</td>
            <td className="px-2 py-0.5">{k.since ? <TimeIST value={k.since} /> : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Histogram({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  if (!entries.length) return <Empty>No checks blocked an order today.</Empty>;
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <ul className="flex flex-col gap-1 p-2 text-xs">
      {entries.map(([code, n]) => (
        <li key={code} className="grid grid-cols-[180px_1fr_40px] items-center gap-2">
          <span className="truncate font-mono text-fg-1">{code}</span>
          <span className="h-2 rounded-sm bg-fg-2" style={{ width: `${(n / max) * 100}%` }} aria-hidden="true" />
          <span className="num text-right text-fg-0">{n}</span>
        </li>
      ))}
    </ul>
  );
}

export default function RiskCenter() {
  const [book, setBook] = useState<string | undefined>("A");
  const risk = useQuery({ queryKey: keys.risk(book), queryFn: ({ signal }) => get("/api/risk", { query: { book }, signal }), refetchInterval: 10_000 });
  const equity = useQuery({ queryKey: ["equity"], queryFn: ({ signal }) => get("/api/equity", { signal }), refetchInterval: 30_000 });
  const view = risk.data?.books[0];
  const curve = useMemo(() => equity.data?.books.find((b) => b.book_id === book)?.points ?? [], [equity.data, book]);
  const lines = useMemo(
    () => [
      { name: "Equity", tone: "accent" as const, points: curve.map((p) => ({ time: p.date, value: Number(p.equity) })) },
      { name: "Peak", tone: "neutral" as const, points: curve.map((p) => ({ time: p.date, value: Number(p.peak) })) },
    ],
    [curve],
  );
  const limits = risk.data?.limits ?? {};

  return (
    <div className="grid grid-cols-1 content-start gap-2 p-2 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <div className="flex flex-col gap-2">
        <Panel title="Limits in use" actions={<BookPicker value={book} onChange={setBook} />}>
          {!view ? (
            <Empty>{risk.isLoading ? "Loading…" : "No risk view."}</Empty>
          ) : (
            <div className="flex flex-col gap-1.5 p-2">
              {view.utilisation.map((u) => <Meter key={u.key} label={u.label} fraction={u.fraction} detail={usageDetail(u)} />)}
              <p className="mt-1 text-2xs text-fg-2">
                {view.valuation === "live" ? "Live, with the RiskEngine's own definitions: 100% is where the check blocks." : "From the stored state and the last mark (heat and sectors need a running session)."}
              </p>
            </div>
          )}
        </Panel>
        <Panel title={`Kill switches · book ${book ?? ""}`} actions={book && <Controls book={book} />}>
          {view ? <Switches view={view} /> : <Empty>—</Empty>}
        </Panel>
        <Panel title="Sector exposure">
          {view && view.sectors.length ? (
            <div className="flex flex-col gap-1.5 p-2">
              {view.sectors.map((u) => <Meter key={u.key} label={u.label} fraction={u.fraction} detail={usageDetail(u)} />)}
            </div>
          ) : (
            <Empty>{view?.valuation === "live" ? "No open positions." : "Sector exposure needs live marks (a running session)."}</Empty>
          )}
        </Panel>
      </div>
      <div className="flex flex-col gap-2">
        <Panel title={`Equity vs peak · book ${book ?? ""} (₹, daily)`}>
          {curve.length ? <Chart label={`Book ${book} equity and running peak, rupees, by session`} lines={lines} height={220} axis="inr" /> : <Empty>No marks yet: the first one is recorded at the end of a session.</Empty>}
        </Panel>
        <Panel title="Blocked checks today (by reason code)">
          <Histogram counts={view?.rejections_today ?? {}} />
        </Panel>
        <Panel title="Event-calendar blocks (entries)">
          {view && view.event_blocks.length ? (
            <table className="w-full text-xs">
              <tbody>
                {view.event_blocks.map((b) => (
                  <tr key={`${b.instrument_key}${b.event_id}`} className="border-b">
                    <td className="px-2 py-0.5">{b.symbol}</td>
                    <td className="px-2 py-0.5 font-mono text-fg-1">{b.code}</td>
                    <td className="px-2 py-0.5"><DateIST value={b.start} /> – <DateIST value={b.end} /></td>
                    <td className="truncate px-2 py-0.5 text-fg-1">{b.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No announcement blocks any entry now.</Empty>
          )}
        </Panel>
        <Panel title={`Limits · ${risk.data?.limits_hash ?? ""}`}>
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 px-2 py-1 text-xs">
            {Object.entries(limits)
              .filter(([, v]) => typeof v === "number" || typeof v === "boolean")
              .map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="truncate font-mono text-fg-1">{k}</dt>
                  <dd className="num text-right text-fg-0">{typeof v === "number" ? num(v, v % 1 ? 4 : 0) : String(v)}</dd>
                </div>
              ))}
          </dl>
        </Panel>
      </div>
    </div>
  );
}
