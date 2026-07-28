import { forwardRef, useMemo, useState } from "react";
import type { CycleTrace, Span, SpanKind } from "../types";
import { clockTime, ms, num, pctPlain, usd } from "../lib/format";
import { Panel } from "./primitives";

const KIND_STYLE: Record<SpanKind, string> = {
  llm: "border-cost/40 bg-cost/10 text-cost",
  deterministic: "border-muted/40 bg-muted/10 text-muted",
  support: "border-amber/40 bg-amber/10 text-amber",
};
const KIND_LABEL: Record<SpanKind, string> = {
  llm: "LLM",
  deterministic: "RULES",
  support: "SUPPORT",
};

function SpanRow({
  span,
  cycleDuration,
  expanded,
  onToggle,
  onOpen,
  focused,
}: {
  span: Span;
  cycleDuration: number;
  expanded: boolean;
  onToggle: () => void;
  onOpen: () => void;
  focused: boolean;
}) {
  const barPct = cycleDuration > 0 && span.latencyMs ? (span.latencyMs / cycleDuration) * 100 : 0;
  return (
    <li className={`border-b border-line/60 ${focused ? "bg-elevated/70" : ""}`}>
      <div className="flex items-center gap-2 px-3 py-1.5 hover:bg-elevated/40">
        <button
          onClick={onToggle}
          aria-label={expanded ? "Collapse span" : "Expand span"}
          className="w-3 shrink-0 text-muted transition-transform"
        >
          {expanded ? "▾" : "▸"}
        </button>

        <button onClick={onOpen} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span
            className={`shrink-0 rounded-sm border px-1 py-0.5 text-[0.6rem] font-bold tracking-wide ${KIND_STYLE[span.kind]}`}
          >
            {KIND_LABEL[span.kind]}
          </span>
          <span className="w-36 shrink-0 truncate text-xs font-medium text-ink">{span.label}</span>
          <span className="min-w-0 flex-1 truncate text-2xs text-muted">
            {span.decision}
            {span.confidence != null && (
              <span className="ml-1 text-amber">· {pctPlain(span.confidence)}</span>
            )}
          </span>
        </button>

        {/* Inline tokens · latency · cost (the span's observability metrics). */}
        <div className="tnum flex shrink-0 items-center gap-3 text-2xs">
          <span className="w-16 text-right text-cost" title="tokens">
            {span.kind === "deterministic" ? "—" : `${num(span.tokens)}t`}
          </span>
          <span className="w-14 text-right text-muted" title="latency">
            {ms(span.latencyMs)}
          </span>
          <span className="w-20 text-right text-cost" title="cost (paid-tier equiv)">
            {span.kind === "deterministic" ? "$0" : usd(span.costUsd, 5)}
          </span>
        </div>
      </div>

      {/* Latency contribution bar */}
      {barPct > 0 && (
        <div className="mx-3 mb-1 h-0.5 rounded-full bg-line/60">
          <div className="h-full rounded-full bg-amber/70" style={{ width: `${barPct}%` }} />
        </div>
      )}

      {expanded && (
        <div className="border-t border-line/40 bg-canvas/50 px-3 py-2 pl-8 text-2xs leading-relaxed text-muted">
          {span.reasoning || <span className="italic">No reasoning recorded for this span.</span>}
          <button onClick={onOpen} className="ml-2 text-cost hover:underline">
            open detail →
          </button>
        </div>
      )}
    </li>
  );
}

interface Props {
  cycle: CycleTrace | null;
  onOpenSpan: (span: Span) => void;
  focusedSpanIndex: number;
}

export const TraceExplorer = forwardRef<HTMLInputElement, Props>(function TraceExplorer(
  { cycle, onOpenSpan, focusedSpanIndex },
  searchRef,
) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const spans = useMemo(() => {
    if (!cycle) return [];
    const q = query.trim().toLowerCase();
    if (!q) return cycle.spans;
    return cycle.spans.filter((s) =>
      `${s.label} ${s.decision} ${s.reasoning}`.toLowerCase().includes(q),
    );
  }, [cycle, query]);

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  return (
    <Panel
      title="Trade-Cycle Trace Explorer"
      accent
      right={
        cycle && (
          <div className="tnum flex items-center gap-3 text-2xs text-muted">
            <span className="text-ink">#{cycle.workflowId.split("-").pop()}</span>
            <span>{clockTime(cycle.ts)}</span>
            <span>{ms(cycle.durationMs)}</span>
            <span className="text-cost">
              {num(cycle.tokens)}t · {usd(cycle.costUsd, 4)}
            </span>
          </div>
        )
      }
      bodyClassName="flex flex-col"
    >
      {!cycle ? (
        <div className="flex h-full items-center justify-center py-8 text-xs italic text-muted">
          Select a cycle to inspect its agent spans
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 border-b bg-elevated/30 px-3 py-1.5">
            <span className="text-muted">/</span>
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="filter spans (press / )"
              aria-label="Filter spans"
              className="w-full bg-transparent text-2xs text-ink placeholder:text-muted/60 focus:outline-none"
              spellCheck={false}
            />
            <span className="tnum shrink-0 text-2xs text-muted">
              {spans.length}/{cycle.spans.length}
            </span>
          </div>
          <ul className="min-h-0 flex-1 overflow-auto">
            {spans.map((span, i) => (
              <SpanRow
                key={span.id}
                span={span}
                cycleDuration={cycle.durationMs}
                expanded={expanded.has(span.id)}
                onToggle={() => toggle(span.id)}
                onOpen={() => onOpenSpan(span)}
                focused={i === focusedSpanIndex}
              />
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
});
