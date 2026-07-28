import type { CycleTrace, Snapshot } from "../types";
import { inrCompact, ms, num, pct, percentile, signedInr, usd } from "../lib/format";
import { Delta, Meter, Panel, Sparkline, StatTile } from "./primitives";

export function KpiMeters({
  snapshot,
  cycles,
  pnlSeries,
}: {
  snapshot: Snapshot;
  cycles: CycleTrace[];
  pnlSeries: number[];
}) {
  const { account, trades, agents, finops, goal } = snapshot;

  const tokenSeries = cycles.slice(-40).map((c) => c.tokens);
  const latencySeries = cycles.slice(-40).map((c) => c.durationMs);
  const durations = cycles.map((c) => c.durationMs);
  const p50 = percentile(durations, 50);
  const p99 = percentile(durations, 99);

  const winFrac = trades.total ? trades.winRate / 100 : 0;
  const winColor =
    trades.winRate >= 50 ? "var(--up)" : trades.winRate >= 40 ? "var(--amber)" : "var(--down)";

  const paceFrac = goal.expectedToDate > 0 ? goal.mtdPnl / goal.expectedToDate : 0;

  return (
    <Panel title="KPI Meters" accent bodyClassName="flex flex-col">
      <StatTile
        label="Net P&L"
        value={<Delta value={account.totalPnl} format={(v) => signedInr(v, 0)} />}
        sub={`${pct(account.pnlPercent)} · realized ${signedInr(account.realizedPnl, 0)}`}
        accent={account.totalPnl >= 0 ? "up" : "down"}
        spark={
          <Sparkline
            data={pnlSeries}
            stroke={account.totalPnl >= 0 ? "var(--up)" : "var(--down)"}
          />
        }
      />

      <StatTile
        label="Win Rate"
        value={`${trades.winRate.toFixed(1)}%`}
        accent={trades.winRate >= 50 ? "up" : "amber"}
        sub={
          <div className="mt-1 flex flex-col gap-1">
            <Meter pct={winFrac} color={winColor} />
            <span>
              {trades.winners}W / {trades.losers}L · {trades.total} closed
            </span>
          </div>
        }
      />

      <StatTile
        label="Trades Executed"
        value={num(trades.total)}
        sub={`${agents.tradesApproved} approved · ${agents.tradesRiskRejected} risk-blocked`}
      />

      <StatTile
        label="Cycles Completed"
        value={num(agents.cyclesRun)}
        sub={`${agents.signalsGenerated} signals · ${agents.approvalRate.toFixed(0)}% approval`}
      />

      <StatTile
        label="Tokens · Cost (today)"
        value={num(finops.tokens)}
        accent="cost"
        sub={`${usd(finops.costUsd)} paid-tier equiv · ${finops.calls} calls`}
        spark={<Sparkline data={tokenSeries} stroke="var(--cost)" />}
      />

      <StatTile
        label="Latency P50 / P99"
        value={
          <span className="tnum">
            {ms(p50)} <span className="text-muted">/</span> {ms(p99)}
          </span>
        }
        sub="per trade-cycle pipeline"
        spark={<Sparkline data={latencySeries} stroke="var(--amber)" />}
      />

      {goal.enabled && (
        <StatTile
          label="Profit-Goal Pace"
          value={
            goal.feasible ? (
              <span className={goal.onPace ? "text-up" : "text-amber"}>
                {goal.onPace ? "▲ on pace" : "▼ behind"}
              </span>
            ) : (
              <span className="text-amber">⚠ infeasible</span>
            )
          }
          accent={goal.onPace ? "up" : "amber"}
          sub={
            <div className="mt-1 flex flex-col gap-1">
              <Meter
                pct={paceFrac}
                color={goal.onPace ? "var(--up)" : "var(--amber)"}
              />
              <span>
                {inrCompact(goal.mtdPnl)} / {inrCompact(goal.expectedToDate)} · advisory only
              </span>
            </div>
          }
        />
      )}
    </Panel>
  );
}
