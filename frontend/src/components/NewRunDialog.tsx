import { useState } from "react";
import type { AppConfig } from "../types";

// Start-run dialog. Demo needs no confirmation; a real run that resolves to the LIVE
// environment requires an explicit confirmation checkbox (mirrors the backend guard —
// the UI never flips allow_live_orders or relaxes a risk gate).
export function NewRunDialog({
  config,
  error,
  onStart,
  onClose,
}: {
  config: AppConfig | null;
  error: string | null;
  onStart: (opts: { demo: boolean; confirmLive: boolean }) => void;
  onClose: () => void;
}) {
  const env = config?.env ?? "PAPER";
  const isLive = env === "LIVE";
  const [confirmLive, setConfirmLive] = useState(false);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-md border bg-panel p-5 shadow-drawer"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold text-ink">Start a run</h3>
        <p className="mt-1 text-2xs text-muted">
          Resolved environment:{" "}
          <span
            className={
              isLive ? "text-down" : env === "SHADOW" ? "text-amber" : "text-up"
            }
          >
            {env}
          </span>
          {config && ` · execution=${config.executionMode} · data=${config.marketDataSource}`}
        </p>

        {error && (
          <div className="mt-3 rounded-sm border border-down/40 bg-down/10 px-3 py-2 text-2xs text-down">
            {error}
          </div>
        )}

        <div className="mt-4 space-y-2">
          <button
            onClick={() => onStart({ demo: true, confirmLive: false })}
            className="flex w-full items-center justify-between rounded-sm border border-cost/40 bg-cost/10 px-3 py-2.5 text-left transition-colors hover:bg-cost/20"
          >
            <div>
              <div className="text-xs font-medium text-cost">Demo run</div>
              <div className="text-2xs text-muted">Synthetic data — no market feed, no orders</div>
            </div>
            <span className="text-cost">▸</span>
          </button>

          <button
            onClick={() => onStart({ demo: false, confirmLive })}
            disabled={isLive && !confirmLive}
            className="flex w-full items-center justify-between rounded-sm border border-amber/40 bg-amber/10 px-3 py-2.5 text-left transition-colors hover:bg-amber/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <div>
              <div className="text-xs font-medium text-amber">Live session</div>
              <div className="text-2xs text-muted">
                Runs the real agent pipeline{" "}
                {isLive ? "— sends REAL orders" : "(paper / shadow — no real orders)"}
              </div>
            </div>
            <span className="text-amber">▸</span>
          </button>
        </div>

        {isLive && (
          <label className="mt-3 flex items-start gap-2 rounded-sm border border-down/40 bg-down/10 px-3 py-2 text-2xs text-down">
            <input
              type="checkbox"
              checked={confirmLive}
              onChange={(e) => setConfirmLive(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              I understand this environment is <b>LIVE</b> and may place real orders. Confirm to
              enable the live session button.
            </span>
          </label>
        )}

        <div className="mt-4 flex justify-end">
          <button
            onClick={onClose}
            className="rounded-sm border px-3 py-1 text-2xs text-muted hover:text-ink"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
