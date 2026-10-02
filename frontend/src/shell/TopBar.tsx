// TopBar (§6.5): product and mode, session state, the session clock (IST), data freshness,
// broker and reconciliation, the books' kill switches, today's AI spend against its cap, HALT.

import { useQuery } from "@tanstack/react-query";
import { get } from "../api/client";
import { keys, useConfig, useSummary, useSystem } from "../api/queries";
import { cx } from "../lib/cx";
import { duration } from "../lib/format";
import { useUi } from "../lib/ui";
import { Badge } from "../ui/Badge";
import { Money, TimeIST } from "../ui/Num";
import { ToolButton } from "../ui/Panel";
import { HaltDialog } from "./HaltDialog";

/** Today's IST date of the session clock (the demo's clock is its tape's day). */
export function sessionDay(now: string | null | undefined): string {
  const d = now ? new Date(now) : new Date();
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(d);
}

function Divider() {
  return <span aria-hidden="true" className="h-4 w-px bg-line-strong" />;
}

export function TopBar() {
  const { data: summary } = useSummary();
  const { data: system } = useSystem();
  const { data: config } = useConfig();
  const { halt, setHalt } = useUi();
  const day = sessionDay(summary?.now);
  const spend = useQuery({
    queryKey: keys.aiSpend({ group_by: "day", since: day, until: day }),
    queryFn: ({ signal }) =>
      get("/api/ai/spend", { query: { group_by: "day", since: day, until: day }, signal }),
    refetchInterval: 30_000,
  });

  const ages = Object.entries(system?.quote_age_s ?? {}).sort((a, b) => a[1] - b[1]);
  const freshest = ages[0];
  const reconciles = system?.last_reconcile ?? [];
  const drift = reconciles.some((r) => !r.in_sync);
  const open = summary?.market_open ?? false;
  const product = (config?.experiment as { product?: string } | undefined)?.product ?? "CNC";
  const budget = config ? Number(config.llm_budget_daily_inr) : null;

  return (
    <header className="flex h-9 shrink-0 items-center gap-3 border-b bg-bg-1 px-3 text-xs">
      <span className="text-sm font-semibold text-fg-0">RakshaQuant</span>
      {summary?.demo ? <Badge tone="warn">DEMO</Badge> : <Badge>{`PAPER · ${product}`}</Badge>}
      <Divider />
      <span className="flex items-center gap-1.5" title="Session state">
        <span aria-hidden="true" className={cx("h-2 w-2 rounded-sm", open ? "bg-accent" : "bg-fg-2")} />
        <span className="text-fg-0">{summary?.session?.state ?? "NO SESSION"}</span>
        <span className="text-fg-2">{open ? "market open" : "market closed"}</span>
      </span>
      <TimeIST value={summary?.now} label className="text-fg-0" />
      <Divider />
      <span className="text-fg-1">
        Data:{" "}
        {freshest ? (
          <span className={cx(freshest[1] > 1800 ? "text-warn" : "text-fg-0")}>
            {freshest[0].toUpperCase()}
            {freshest[1] > 60 && ` delayed ~${duration(freshest[1])}`}
          </span>
        ) : (
          <span className="text-fg-2">no quotes</span>
        )}
      </span>
      <span className="text-fg-1">
        Broker: <span className="text-fg-0">SIM</span>{" "}
        {reconciles.length === 0 ? (
          <span className="text-fg-2">not reconciled</span>
        ) : drift ? (
          <Badge tone="warn">drift</Badge>
        ) : (
          <span className="text-fg-0">in sync</span>
        )}
      </span>
      <span className="flex items-center gap-1 text-fg-1">
        Books
        {summary?.books.map((b) => (
          <Badge
            key={b.book_id}
            tone={b.kill_switch === "ARMED" ? "neutral" : b.kill_switch === "FLATTEN" ? "crit" : "warn"}
            title={`Book ${b.book_id}: ${b.advisor}, kill switch ${b.kill_switch}`}
          >
            {b.kill_switch === "ARMED" ? b.book_id : `${b.book_id} ${b.kill_switch === "HALT_NEW" ? "HALT" : b.kill_switch}`}
          </Badge>
        ))}
      </span>
      <span className="ml-auto text-fg-1">
        AI today <Money value={spend.data?.total_inr} className="text-fg-0" /> /{" "}
        {budget === null ? "—" : budget > 0 ? <Money value={budget} /> : "no cap"}
      </span>
      <ToolButton tone="crit" onClick={() => setHalt(true)} title="Block new entries in every book">
        HALT
      </ToolButton>
      <HaltDialog open={halt} onOpenChange={setHalt} />
    </header>
  );
}
