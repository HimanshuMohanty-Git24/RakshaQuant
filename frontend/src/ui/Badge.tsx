// Status badges (§6.4). Colour is semantic only (§6.1): green/red are reserved for P&L and
// BUY/SELL, amber marks warnings and degraded states, solid red is critical (kill switches).
// The text always carries the meaning.

import type { ReactNode } from "react";

import { cx } from "../lib/cx";

export type Tone = "neutral" | "up" | "down" | "warn" | "crit" | "accent";

const STYLE: Record<Tone, string> = {
  neutral: "border-line-strong text-fg-1",
  up: "border-up text-up",
  down: "border-down text-down",
  warn: "border-warn text-warn",
  crit: "border-crit-bg bg-crit-bg text-crit-fg",
  accent: "border-accent text-accent",
};

const TONE_OF: Record<string, Tone> = {
  BUY: "up",
  SELL: "down",
  VETO: "warn",
  VETOED: "warn",
  ABSTAIN: "warn",
  DEGRADED: "warn",
  DEMO: "warn",
  HALT_NEW: "warn",
  HALTED: "warn",
  REJECTED: "warn",
  RISK_REJECTED: "warn",
  BROKER_REJECTED: "warn",
  UNKNOWN: "warn",
  RESIZED: "warn",
  FLATTEN: "crit",
  CRITICAL: "crit",
  WARNING: "warn",
};

/** The tone a status word gets unless one is given. */
export function toneOf(status: string): Tone {
  return TONE_OF[status.toUpperCase()] ?? "neutral";
}

export function Badge({
  children,
  tone,
  title,
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
  className?: string;
}) {
  const resolved = tone ?? (typeof children === "string" ? toneOf(children) : "neutral");
  return (
    <span
      title={title}
      className={cx(
        "inline-flex h-[18px] items-center whitespace-nowrap rounded-sm border px-1 font-mono text-2xs uppercase leading-none",
        STYLE[resolved],
        className,
      )}
    >
      {children}
    </span>
  );
}
