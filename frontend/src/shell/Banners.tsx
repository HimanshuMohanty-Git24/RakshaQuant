import { useSummary } from "../api/queries";
import { dateIST } from "../lib/format";
import { useStream } from "../lib/stream";

/** A persistent banner whenever the console shows the demo (§6.1). */
export function DemoBanner() {
  const { data: summary } = useSummary();
  if (!summary?.demo) return null;
  return (
    <div role="note" className="shrink-0 border-b border-warn bg-bg-1 px-3 py-1 text-xs text-warn">
      DEMO — the bundled fixture tape ({dateIST(summary.session?.date)}, synthetic prices) replayed
      through the real engine. Nothing here is a real market or a real order.
    </div>
  );
}

/** A wrong or expired token: say what to do, nothing else (no data is shown). */
export function Unauthorized() {
  return (
    <div className="flex h-full items-center justify-center bg-bg-0 p-6">
      <div className="max-w-md rounded border bg-bg-1 p-4">
        <h1 className="text-sm font-medium text-fg-0">Not authorised</h1>
        <p className="mt-2 text-xs text-fg-1">
          This console needs the access token of the running server. Open the link the server
          printed when it started (it ends in <span className="font-mono">#token=…</span>). A
          restart issues a new token.
        </p>
      </div>
    </div>
  );
}

export function useUnauthorized(): boolean {
  return useStream((s) => s.status === "unauthorized");
}
