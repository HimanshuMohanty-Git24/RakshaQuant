import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CycleTrace } from "../types";
import { clockTime, num, usd } from "../lib/format";
import { Dot, Panel } from "./primitives";

const ROW_H = 58;
const OVERSCAN = 5;

const REGIME_LABEL: Record<string, string> = {
  trending_up: "BULL",
  trending_down: "BEAR",
  ranging: "RANGE",
  volatile: "VOL",
  unknown: "—",
};

// Newest-first list of trade cycles (traces). Windowed so a long run stays smooth: only the
// rows in view (± overscan) are mounted, with spacer divs preserving scroll height.
export function RunSelector({
  cycles,
  selectedId,
  onSelect,
}: {
  cycles: CycleTrace[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const list = [...cycles].reverse();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(480);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportH(el.clientHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Keep the selected row in view when it changes via keyboard.
  useEffect(() => {
    if (!selectedId) return;
    const idx = list.findIndex((c) => c.id === selectedId);
    if (idx < 0) return;
    const el = scrollRef.current;
    if (!el) return;
    const top = idx * ROW_H;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + ROW_H > el.scrollTop + el.clientHeight)
      el.scrollTop = top + ROW_H - el.clientHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const total = list.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewportH) / ROW_H) + OVERSCAN);
  const slice = list.slice(start, end);

  return (
    <Panel
      title="Run Selector"
      right={<span className="text-2xs text-muted">{total} cycles</span>}
      bodyClassName="p-0"
    >
      {total === 0 ? (
        <div className="flex h-full items-center justify-center px-3 py-6 text-center text-xs italic text-muted">
          No cycles yet — start a run
        </div>
      ) : (
        <div
          ref={scrollRef}
          className="h-full overflow-auto"
          onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        >
          <div style={{ height: total * ROW_H, position: "relative" }}>
            <div style={{ transform: `translateY(${start * ROW_H}px)` }}>
              {slice.map((c) => {
                const active = c.id === selectedId;
                const err = c.status !== "success";
                return (
                  <button
                    key={c.id}
                    onClick={() => onSelect(c.id)}
                    style={{ height: ROW_H }}
                    className={`flex w-full flex-col justify-center gap-0.5 border-b border-line/60 px-3 text-left transition-colors ${
                      active
                        ? "border-l-2 border-l-amber bg-elevated"
                        : "border-l-2 border-l-transparent hover:bg-elevated/50"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1.5 text-xs font-medium text-ink">
                        <Dot className={err ? "bg-down" : "bg-up"} />#{c.workflowId.split("-").pop()}
                      </span>
                      <span className="tnum text-2xs text-muted">{clockTime(c.ts)}</span>
                    </div>
                    <div className="flex items-center justify-between text-2xs text-muted">
                      <span className="rounded-sm bg-line/60 px-1 text-ink/80">
                        {REGIME_LABEL[c.regime] ?? c.regime}
                      </span>
                      <span className="tnum">
                        <span className="text-up">{c.approvedCount}✓</span>
                        {" · "}
                        <span className="text-down">{c.rejectedCount}✗</span>
                      </span>
                      <span className="tnum text-cost" title="tokens / cost">
                        {num(c.tokens)}t · {usd(c.costUsd, 4)}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
