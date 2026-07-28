import { forwardRef, useEffect, useState } from "react";
import type { ConnState, EnvBadge, RunStatus } from "../types";
import { Dot } from "./primitives";

const ENV_STYLE: Record<EnvBadge, string> = {
  PAPER: "border-up/40 bg-up/10 text-up",
  SHADOW: "border-amber/40 bg-amber/10 text-amber",
  LIVE: "border-down/50 bg-down/15 text-down",
};

function EnvChip({ env }: { env: EnvBadge }) {
  return (
    <span
      className={`rounded-sm border px-2 py-0.5 text-2xs font-bold tracking-widest ${ENV_STYLE[env]}`}
      title={
        env === "LIVE"
          ? "LIVE — real orders (gated by allow_live_orders)"
          : env === "SHADOW"
            ? "SHADOW — mirrors decisions, sends no real orders"
            : "PAPER — virtual wallet"
      }
    >
      {env}
    </span>
  );
}

function StatusPill({ status, cycle }: { status: RunStatus; cycle: number }) {
  if (status === "RUNNING") {
    return (
      <span className="flex items-center gap-1.5 text-amber">
        <Dot className="bg-amber" pulse />
        RUNNING · cycle {cycle}
      </span>
    );
  }
  const map: Record<RunStatus, { c: string; t: string }> = {
    IDLE: { c: "bg-muted", t: "IDLE" },
    RUNNING: { c: "bg-amber", t: "RUNNING" },
    DONE: { c: "bg-muted", t: "STOPPED" },
    ERROR: { c: "bg-down", t: "ERROR" },
  };
  const s = map[status] ?? map.IDLE;
  return (
    <span className="flex items-center gap-1.5 text-muted">
      <Dot className={s.c} />
      {s.t}
    </span>
  );
}

function useClock(): string {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  // Render the wall clock in IST (the market timezone), independent of the viewer's locale.
  return now.toLocaleTimeString("en-GB", { hour12: false, timeZone: "Asia/Kolkata" });
}

interface Props {
  env: EnvBadge;
  status: RunStatus;
  cycle: number;
  conn: ConnState;
  demo: boolean;
  running: boolean;
  onCommand: (raw: string) => void;
  onNewRun: () => void;
  onStop: () => void;
}

export const CommandBar = forwardRef<HTMLInputElement, Props>(function CommandBar(
  { env, status, cycle, conn, demo, running, onCommand, onNewRun, onStop },
  ref,
) {
  const [cmd, setCmd] = useState("");
  const clock = useClock();

  const connLabel: Record<ConnState, { c: string; t: string }> = {
    open: { c: "bg-up", t: "live" },
    connecting: { c: "bg-amber", t: "connecting" },
    reconnecting: { c: "bg-amber", t: "reconnecting" },
    closed: { c: "bg-down", t: "offline" },
  };

  return (
    <header className="flex items-center gap-3 border-b bg-panel px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-bold tracking-tight text-ink">
          Raksha<span className="text-amber">Quant</span>
        </span>
        <EnvChip env={env} />
        {demo && (
          <span className="rounded-sm border border-cost/40 bg-cost/10 px-1.5 py-0.5 text-2xs font-medium text-cost">
            DEMO
          </span>
        )}
      </div>

      <div className="mx-1 h-4 w-px bg-line" />

      <div className="text-xs">
        <StatusPill status={status} cycle={cycle} />
      </div>

      <div className="ml-auto flex items-center gap-3">
        <span className="flex items-center gap-1.5 text-2xs text-muted" title="WebSocket status">
          <Dot className={connLabel[conn].c} pulse={conn === "reconnecting"} />
          {connLabel[conn].t}
        </span>

        <span className="tnum text-xs text-muted" title="Indian Standard Time">
          {clock} IST
        </span>

        <form
          className="flex items-center gap-1 rounded-sm border bg-canvas px-2 py-1"
          onSubmit={(e) => {
            e.preventDefault();
            const v = cmd.trim();
            if (v) onCommand(v);
            setCmd("");
          }}
        >
          <span className="text-amber">:</span>
          <input
            ref={ref}
            value={cmd}
            onChange={(e) => setCmd(e.target.value)}
            placeholder="command  (press : )"
            aria-label="Command input"
            className="w-40 bg-transparent text-xs text-ink placeholder:text-muted/60 focus:outline-none"
            spellCheck={false}
            autoComplete="off"
          />
        </form>

        {running ? (
          <button
            onClick={onStop}
            className="rounded-sm border border-down/50 bg-down/10 px-3 py-1 text-xs font-medium text-down transition-colors hover:bg-down/20"
          >
            ■ Stop
          </button>
        ) : (
          <button
            onClick={onNewRun}
            className="rounded-sm border border-amber/50 bg-amber/10 px-3 py-1 text-xs font-medium text-amber transition-colors hover:bg-amber/20"
          >
            ▸ New Run
          </button>
        )}
      </div>
    </header>
  );
});
