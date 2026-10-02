import type { ReactNode } from "react";

import { cx } from "../lib/cx";

export interface Step {
  id: string;
  title: ReactNode;
  meta?: ReactNode; // e.g. a time or a badge
  tone?: "neutral" | "warn" | "crit" | "accent";
  children?: ReactNode;
}

const DOT = {
  neutral: "border-line-strong bg-bg-2",
  warn: "border-warn bg-bg-2",
  crit: "border-crit-bg bg-crit-bg",
  accent: "border-accent bg-bg-2",
};

/** Vertical lineage steps (the Decision Inspector, §6.5). */
export function Timeline({ steps }: { steps: Step[] }) {
  return (
    <ol className="relative ml-2 border-l border-line-strong">
      {steps.map((step) => (
        <li key={step.id} className="relative pb-4 pl-4 last:pb-0">
          <span
            aria-hidden="true"
            className={cx("absolute -left-[5px] top-1.5 h-[9px] w-[9px] rounded-sm border", DOT[step.tone ?? "neutral"])}
          />
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="text-sm font-medium text-fg-0">{step.title}</h3>
            {step.meta && <div className="shrink-0 text-xs text-fg-2">{step.meta}</div>}
          </div>
          {step.children && <div className="mt-1 text-xs text-fg-1">{step.children}</div>}
        </li>
      ))}
    </ol>
  );
}
