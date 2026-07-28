import { useEffect, useRef, useState } from "react";
import type { ActivityEntry, ActivityLevel } from "../types";
import { Panel } from "./primitives";

const LEVEL: Record<ActivityLevel, { c: string; tag: string }> = {
  INFO: { c: "text-cost", tag: "INFO" },
  SUCCESS: { c: "text-up", tag: " OK " },
  WARNING: { c: "text-amber", tag: "WARN" },
  ERROR: { c: "text-down", tag: "ERR " },
  TRADE: { c: "text-amber", tag: "TRDE" },
};

// The direct evolution of the CLI's stdout: a monospace event stream with ts + [LEVEL] tags.
// Autoscrolls while pinned to the bottom; scrolling up pauses it and shows a "resume" pill.
export function LiveFeed({ activity }: { activity: ActivityEntry[] }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  useEffect(() => {
    if (pinned && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [activity, pinned]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    setPinned(atBottom);
  };

  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setPinned(true);
  };

  return (
    <Panel
      title="Live Agent Feed"
      right={
        <span className="flex items-center gap-1 text-2xs text-muted">
          <span className={`h-1.5 w-1.5 rounded-full ${pinned ? "bg-up" : "bg-muted"}`} />
          {pinned ? "autoscroll" : "paused"}
        </span>
      }
      bodyClassName="relative"
    >
      <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-auto px-3 py-1.5">
        {activity.length === 0 ? (
          <div className="py-4 text-xs italic text-muted">Waiting for activity…</div>
        ) : (
          <ul className="space-y-0.5">
            {activity.map((e, i) => {
              const lv = LEVEL[e.level] ?? LEVEL.INFO;
              return (
                <li key={i} className="flex gap-2 text-2xs leading-relaxed">
                  <span className="tnum shrink-0 text-muted/70">{e.time}</span>
                  <span className={`shrink-0 font-bold ${lv.c}`}>[{lv.tag}]</span>
                  <span className="min-w-0 break-words text-ink/90">{e.message}</span>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {!pinned && (
        <button
          onClick={jumpToLatest}
          className="absolute bottom-2 right-3 rounded-full border border-amber/50 bg-panel px-2.5 py-1 text-2xs text-amber shadow-drawer hover:bg-amber/10"
        >
          ↓ latest
        </button>
      )}
    </Panel>
  );
}
