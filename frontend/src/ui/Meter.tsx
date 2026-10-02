// A utilisation bar (§6.4): used against a limit, amber from 70% and red from 90%. The
// percentage is always written next to the bar.

import type { ReactNode } from "react";

import { cx } from "../lib/cx";

export function meterTone(fraction: number | null): "ok" | "warn" | "crit" {
  if (fraction === null) return "ok";
  if (fraction >= 0.9) return "crit";
  if (fraction >= 0.7) return "warn";
  return "ok";
}

const BAR = { ok: "bg-fg-2", warn: "bg-warn", crit: "bg-down" } as const;
const TEXT = { ok: "text-fg-1", warn: "text-warn", crit: "text-down" } as const;

export function Meter({
  label,
  fraction,
  detail,
  className,
}: {
  label: string;
  /** used / limit, from the server (the UI does not compute money). */
  fraction: number | null;
  /** e.g. "₹4.1k / ₹10k" */
  detail?: ReactNode;
  className?: string;
}) {
  const tone = meterTone(fraction);
  const shown = fraction === null ? null : Math.max(0, Math.min(1, fraction));
  return (
    <div className={cx("grid grid-cols-[96px_1fr_auto] items-center gap-2 text-xs", className)}>
      <span className="truncate text-fg-1">{label}</span>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={shown === null ? undefined : Math.round(shown * 100)}
        aria-valuetext={fraction === null ? "no limit" : `${Math.round(fraction * 100)}%`}
        className="h-1.5 overflow-hidden rounded-sm bg-bg-2"
      >
        {shown !== null && (
          <div className={cx("h-full", BAR[tone])} style={{ width: `${shown * 100}%` }} />
        )}
      </div>
      <span className={cx("num whitespace-nowrap", TEXT[tone])}>
        {detail}
        <span className="ml-2 inline-block w-10 text-right">
          {fraction === null ? "—" : `${Math.round(fraction * 100)}%`}
        </span>
      </span>
    </div>
  );
}
