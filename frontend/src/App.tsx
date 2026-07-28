import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AppConfig, CycleTrace, RunStatus, Span } from "./types";
import { useLiveState } from "./lib/useLiveState";
import { fetchConfig, startRun, stopRun } from "./lib/api";
import { CommandBar } from "./components/CommandBar";
import { RunSelector } from "./components/RunSelector";
import { PositionsPanel } from "./components/PositionsPanel";
import { TraceExplorer } from "./components/TraceExplorer";
import { LiveFeed } from "./components/LiveFeed";
import { KpiMeters } from "./components/KpiMeters";
import { RegimePanel } from "./components/RegimePanel";
import { DetailDrawer } from "./components/DetailDrawer";
import { ShortcutHelp } from "./components/ShortcutHelp";
import { NewRunDialog } from "./components/NewRunDialog";

const PNL_SERIES_MAX = 60;

export default function App() {
  const { snapshot, cycles, running, demo, conn, error } = useLiveState();
  const [config, setConfig] = useState<AppConfig | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pinnedLatest, setPinnedLatest] = useState(true);
  const [openSpan, setOpenSpan] = useState<Span | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [pnlSeries, setPnlSeries] = useState<number[]>([]);

  const commandRef = useRef<HTMLInputElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const traceRef = useRef<HTMLDivElement>(null);
  const feedRef = useRef<HTMLDivElement>(null);
  const gPending = useRef(false);
  const lastPnlTs = useRef<string>("");

  useEffect(() => {
    fetchConfig().then(setConfig);
  }, []);

  // Keep a rolling P&L series for the KPI sparkline (sampled per snapshot).
  useEffect(() => {
    if (!snapshot || snapshot.ts === lastPnlTs.current) return;
    lastPnlTs.current = snapshot.ts;
    setPnlSeries((prev) => {
      const next = [...prev, snapshot.account.totalPnl];
      return next.length > PNL_SERIES_MAX ? next.slice(-PNL_SERIES_MAX) : next;
    });
  }, [snapshot]);

  // Newest-first cycle list + selection that follows the latest until the user picks one.
  const reversed = useMemo(() => [...cycles].reverse(), [cycles]);
  const latestId = reversed[0]?.id ?? null;

  useEffect(() => {
    if (pinnedLatest && latestId) setSelectedId(latestId);
  }, [pinnedLatest, latestId]);

  const selectedCycle: CycleTrace | null = useMemo(
    () => cycles.find((c) => c.id === selectedId) ?? reversed[0] ?? null,
    [cycles, selectedId, reversed],
  );

  const selectCycle = useCallback(
    (id: string) => {
      setSelectedId(id);
      setPinnedLatest(id === latestId);
    },
    [latestId],
  );

  const moveCursor = useCallback(
    (delta: number) => {
      if (reversed.length === 0) return;
      const idx = reversed.findIndex((c) => c.id === (selectedId ?? latestId));
      const next = Math.max(0, Math.min(reversed.length - 1, (idx < 0 ? 0 : idx) + delta));
      selectCycle(reversed[next].id);
    },
    [reversed, selectedId, latestId, selectCycle],
  );

  const handleStart = useCallback(async (opts: { demo: boolean; confirmLive: boolean }) => {
    const res = await startRun(opts);
    if (res.ok) {
      setDialogOpen(false);
      setStartError(null);
    } else {
      setStartError(res.error ?? "Failed to start");
    }
  }, []);

  const runCommand = useCallback(
    (raw: string) => {
      const cmd = raw.toLowerCase().replace(/^:/, "").trim();
      if (cmd === "start" || cmd === "run") void handleStart({ demo: false, confirmLive: false });
      else if (cmd === "demo") void handleStart({ demo: true, confirmLive: false });
      else if (cmd === "stop") void stopRun();
      else if (cmd === "help" || cmd === "?") setHelpOpen(true);
      else if (cmd === "clear") setOpenSpan(null);
    },
    [handleStart],
  );

  // Keyboard-first navigation (power users came from a CLI).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement;
      const typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");

      if (e.key === "Escape") {
        if (openSpan) setOpenSpan(null);
        else if (helpOpen) setHelpOpen(false);
        else if (dialogOpen) setDialogOpen(false);
        (document.activeElement as HTMLElement)?.blur?.();
        return;
      }
      if (typing) return;

      if (gPending.current) {
        gPending.current = false;
        if (e.key === "t") traceRef.current?.scrollIntoView({ behavior: "smooth" });
        if (e.key === "f") feedRef.current?.scrollIntoView({ behavior: "smooth" });
        return;
      }

      switch (e.key) {
        case "j":
          e.preventDefault();
          moveCursor(1);
          break;
        case "k":
          e.preventDefault();
          moveCursor(-1);
          break;
        case "Enter":
          if (selectedCycle?.spans.length) setOpenSpan(selectedCycle.spans[0]);
          break;
        case "/":
          e.preventDefault();
          searchRef.current?.focus();
          break;
        case ":":
          e.preventDefault();
          commandRef.current?.focus();
          break;
        case "?":
          setHelpOpen((v) => !v);
          break;
        case "g":
          gPending.current = true;
          window.setTimeout(() => (gPending.current = false), 800);
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moveCursor, selectedCycle, openSpan, helpOpen, dialogOpen]);

  const env = snapshot?.run.env ?? config?.env ?? "PAPER";
  const status: RunStatus = error ? "ERROR" : running ? "RUNNING" : snapshot ? "IDLE" : "IDLE";

  const loading = !snapshot && conn === "connecting";

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-canvas text-ink">
      <CommandBar
        ref={commandRef}
        env={env}
        status={status}
        cycle={snapshot?.run.cycle ?? 0}
        conn={conn}
        demo={demo}
        running={running}
        onCommand={runCommand}
        onNewRun={() => {
          setStartError(null);
          setDialogOpen(true);
        }}
        onStop={() => void stopRun()}
      />

      {conn === "reconnecting" && (
        <div className="flex items-center gap-2 border-b border-amber/30 bg-amber/10 px-3 py-1 text-2xs text-amber">
          <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-amber" />
          Connection lost — reconnecting to the trading server…
        </div>
      )}
      {error && (
        <div className="border-b border-down/30 bg-down/10 px-3 py-1 text-2xs text-down">
          Session error: {error}
        </div>
      )}

      {loading ? (
        <LoadingState />
      ) : !snapshot ? (
        <EmptyState onNewRun={() => setDialogOpen(true)} />
      ) : (
        <main className="grid min-h-0 flex-1 grid-cols-12 gap-2 p-2">
          {/* Left rail — run selector */}
          <div className="col-span-3 flex min-h-0 flex-col xl:col-span-2">
            <RunSelector cycles={cycles} selectedId={selectedId} onSelect={selectCycle} />
          </div>

          {/* Center — positions, trace explorer, live feed */}
          <div className="col-span-6 grid min-h-0 grid-rows-[minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1fr)] gap-2 xl:col-span-7">
            <PositionsPanel snapshot={snapshot} />
            <div ref={traceRef} className="min-h-0">
              <TraceExplorer
                ref={searchRef}
                cycle={selectedCycle}
                onOpenSpan={setOpenSpan}
                focusedSpanIndex={-1}
              />
            </div>
            <div ref={feedRef} className="min-h-0">
              <LiveFeed activity={snapshot.activity} />
            </div>
          </div>

          {/* Right rail — regime/decision + KPI meters */}
          <div className="col-span-3 grid min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-2">
            <RegimePanel regime={snapshot.regime} decision={snapshot.decision} />
            <KpiMeters snapshot={snapshot} cycles={cycles} pnlSeries={pnlSeries} />
          </div>
        </main>
      )}

      {openSpan && <DetailDrawer span={openSpan} onClose={() => setOpenSpan(null)} />}
      {helpOpen && <ShortcutHelp onClose={() => setHelpOpen(false)} />}
      {dialogOpen && (
        <NewRunDialog
          config={config}
          error={startError}
          onStart={handleStart}
          onClose={() => setDialogOpen(false)}
        />
      )}

      <footer className="flex items-center justify-between border-t bg-panel px-3 py-1 text-[0.6rem] text-muted">
        <span>RakshaQuant — educational paper-trading. Not investment advice.</span>
        <button onClick={() => setHelpOpen(true)} className="hover:text-amber">
          press <kbd className="text-amber">?</kbd> for shortcuts
        </button>
      </footer>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="grid flex-1 grid-cols-12 gap-2 p-2">
      <div className="skeleton col-span-3 rounded-md xl:col-span-2" />
      <div className="col-span-6 grid grid-rows-3 gap-2 xl:col-span-7">
        <div className="skeleton rounded-md" />
        <div className="skeleton rounded-md" />
        <div className="skeleton rounded-md" />
      </div>
      <div className="skeleton col-span-3 rounded-md" />
    </div>
  );
}

function EmptyState({ onNewRun }: { onNewRun: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center">
      <div className="text-5xl text-amber/70">▸</div>
      <div>
        <h2 className="text-lg font-semibold text-ink">No runs yet</h2>
        <p className="mt-1 max-w-sm text-xs text-muted">
          Start a session to watch the agent pipeline classify the regime, pick strategies,
          validate signals, and size trades — cycle by cycle.
        </p>
      </div>
      <button
        onClick={onNewRun}
        className="rounded-sm border border-amber/50 bg-amber/10 px-4 py-2 text-xs font-medium text-amber hover:bg-amber/20"
      >
        ▸ Start a run
      </button>
    </div>
  );
}
