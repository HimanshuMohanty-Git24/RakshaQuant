import type { Snapshot } from "../types";
import { inr, pct, signedInr } from "../lib/format";
import { Delta, Panel, SideTag } from "./primitives";

export function PositionsPanel({ snapshot }: { snapshot: Snapshot }) {
  const { account, positions, quotes } = snapshot;
  const movers = quotes.slice(0, 6);

  return (
    <Panel
      title="Positions & P&L"
      right={
        <div className="flex items-center gap-4 text-2xs">
          <span className="text-muted">
            Bal <span className="tnum text-ink">{inr(account.currentBalance, 0)}</span>
          </span>
          <span className="text-muted">
            P&L{" "}
            <Delta
              value={account.totalPnl}
              format={(v) => `${signedInr(v, 0)} (${pct(account.pnlPercent)})`}
            />
          </span>
        </div>
      }
      bodyClassName="flex flex-col"
    >
      {/* Realized / unrealized split strip */}
      <div className="grid grid-cols-2 gap-px border-b bg-line/40 text-2xs">
        <div className="flex items-center justify-between bg-panel px-3 py-1.5">
          <span className="text-muted">Realized</span>
          <Delta value={account.realizedPnl} format={(v) => signedInr(v, 0)} showArrow={false} />
        </div>
        <div className="flex items-center justify-between bg-panel px-3 py-1.5">
          <span className="text-muted">Unrealized</span>
          <Delta value={account.unrealizedPnl} format={(v) => signedInr(v, 0)} showArrow={false} />
        </div>
      </div>

      {/* Positions table */}
      <div className="min-h-0 flex-1 overflow-auto">
        {positions.length === 0 ? (
          <div className="flex h-full items-center justify-center py-6 text-xs italic text-muted">
            No open positions
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-panel">
              <tr className="border-b text-2xs uppercase tracking-wider text-muted">
                <th className="px-3 py-1.5 text-left font-medium">Symbol</th>
                <th className="px-2 py-1.5 text-left font-medium">Side</th>
                <th className="px-2 py-1.5 text-right font-medium">Qty</th>
                <th className="px-2 py-1.5 text-right font-medium">Entry</th>
                <th className="px-3 py-1.5 text-right font-medium">P&L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => (
                <tr
                  key={`${p.symbol}-${i}`}
                  className="border-b border-line/50 hover:bg-elevated/50"
                >
                  <td className="px-3 py-1.5 font-medium text-ink">{p.symbol}</td>
                  <td className="px-2 py-1.5">
                    <SideTag side={p.side} />
                  </td>
                  <td className="tnum px-2 py-1.5 text-right text-ink">{p.qty}</td>
                  <td className="tnum px-2 py-1.5 text-right text-muted">{inr(p.entry, 2)}</td>
                  <td className="px-3 py-1.5 text-right">
                    <Delta value={p.pnl} format={(v) => signedInr(v, 0)} showArrow={false} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Movers ticker */}
      {movers.length > 0 && (
        <div className="flex items-center gap-4 overflow-x-auto border-t bg-elevated/30 px-3 py-1.5 text-2xs">
          <span className="shrink-0 uppercase tracking-wider text-muted">Movers</span>
          {movers.map((q) => {
            const up = q.changePercent >= 0;
            return (
              <span key={q.symbol} className="tnum flex shrink-0 items-center gap-1">
                <span className="text-ink">{q.symbol}</span>
                <span className={up ? "text-up" : "text-down"}>
                  {up ? "▲" : "▼"} {pct(q.changePercent)}
                </span>
              </span>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
