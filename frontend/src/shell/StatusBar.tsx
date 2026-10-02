// StatusBar (§6.5): loop lag, the freshest quote, events, the stream connection, build, env,
// and the single-key shortcut toggle.

import { useSummary, useSystem } from "../api/queries";
import { cx } from "../lib/cx";
import { duration, num } from "../lib/format";
import { useSettings } from "../lib/settings";
import { useStream } from "../lib/stream";

const WS_TEXT = {
  open: "live",
  connecting: "connecting",
  reconnecting: "reconnecting",
  unauthorized: "unauthorised",
} as const;

export function StatusBar() {
  const { data: system } = useSystem();
  const { data: summary } = useSummary();
  const status = useStream((s) => s.status);
  const { shortcuts, toggleShortcuts } = useSettings();
  const ages = Object.values(system?.quote_age_s ?? {});
  const freshest = ages.length ? Math.min(...ages) : null;
  const lag = system?.loop_lag_ms;

  return (
    <footer className="flex h-6 shrink-0 items-center gap-4 border-t bg-bg-1 px-3 text-2xs text-fg-2">
      <span>
        loop lag{" "}
        <span className={cx("num", lag !== null && lag !== undefined && lag > 500 ? "text-warn" : "text-fg-1")}>
          {lag === null || lag === undefined ? "—" : `${num(lag, 0)} ms`}
        </span>
      </span>
      <span>
        last quote <span className="num text-fg-1">{freshest === null ? "—" : `${duration(freshest)} old`}</span>
      </span>
      <span>
        events <span className="num text-fg-1">{num(summary?.last_seq ?? null, 0)}</span>
      </span>
      <span>
        stream{" "}
        <span className={cx(status === "open" ? "text-fg-1" : "text-warn")}>
          <span aria-hidden="true">● </span>
          {WS_TEXT[status]}
        </span>
      </span>
      <span>
        build <span className="font-mono text-fg-1">{system?.versions.build ?? system?.versions.app ?? "—"}</span>
      </span>
      <span>
        env <span className="text-fg-1">{system?.environment ?? "—"}</span>
      </span>
      <button
        type="button"
        onClick={toggleShortcuts}
        aria-pressed={shortcuts}
        className="ml-auto hover:text-fg-0"
        title="Single-key shortcuts (1-8 switch screens). Ctrl/Cmd+K always opens the palette."
      >
        keys: <span className="text-fg-1">{shortcuts ? "on" : "off"}</span>
      </button>
      <span>
        <kbd className="font-mono text-fg-1">Ctrl K</kbd> palette
      </span>
    </footer>
  );
}
