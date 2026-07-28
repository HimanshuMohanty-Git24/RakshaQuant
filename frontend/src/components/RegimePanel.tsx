import type { Regime, Decision } from "../types";
import { pctPlain } from "../lib/format";
import { Meter, Panel } from "./primitives";

const REGIME: Record<string, { label: string; color: string }> = {
  trending_up: { label: "BULL / TRENDING UP", color: "var(--up)" },
  trending_down: { label: "BEAR / TRENDING DOWN", color: "var(--down)" },
  ranging: { label: "RANGING", color: "var(--amber)" },
  volatile: { label: "VOLATILE", color: "#b57bff" },
  unknown: { label: "UNKNOWN", color: "var(--muted)" },
};

export function RegimePanel({ regime, decision }: { regime: Regime; decision: Decision }) {
  const r = REGIME[regime.current] ?? REGIME.unknown;
  const buy = decision.signalType.toUpperCase() === "BUY";

  return (
    <Panel title="Market Regime & Decision" bodyClassName="flex flex-col gap-3 p-3">
      <div>
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold" style={{ color: r.color }}>
            {r.label}
          </span>
          <span className="tnum text-2xs text-muted">
            conf {pctPlain(regime.confidence)}
          </span>
        </div>
        <div className="mt-1.5">
          <Meter pct={regime.confidence} color={r.color} />
        </div>
      </div>

      <div>
        <div className="text-2xs uppercase tracking-wider text-muted">Active Strategies</div>
        <div className="mt-1 flex flex-wrap gap-1">
          {regime.strategies.length ? (
            regime.strategies.map((s) => (
              <span
                key={s}
                className="rounded-sm border border-cost/30 bg-cost/10 px-1.5 py-0.5 text-2xs text-cost"
              >
                {s}
              </span>
            ))
          ) : (
            <span className="text-2xs italic text-muted">none active</span>
          )}
        </div>
      </div>

      <div className="border-t border-line/60 pt-2">
        <div className="text-2xs uppercase tracking-wider text-muted">Current Signal</div>
        {decision.symbol ? (
          <div className="mt-1 flex items-center gap-2 text-xs">
            <span className={`font-bold ${buy ? "text-up" : "text-down"}`}>
              {buy ? "▲ BUY" : "▼ SELL"}
            </span>
            <span className="font-medium text-ink">{decision.symbol}</span>
            <span className="text-muted">{decision.strategy}</span>
            {decision.confidence > 0 && (
              <span className="ml-auto tnum text-amber">{pctPlain(decision.confidence)}</span>
            )}
          </div>
        ) : (
          <div className="mt-1 text-2xs italic text-muted">No active signal</div>
        )}
        {decision.reason && (
          <p className="mt-1.5 font-sans text-2xs leading-relaxed text-muted">{decision.reason}</p>
        )}
      </div>
    </Panel>
  );
}
