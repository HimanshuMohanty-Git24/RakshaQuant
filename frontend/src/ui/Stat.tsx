import type { ReactNode } from "react";

import { cx } from "../lib/cx";

/** Label (11 px, muted) over a value (16 px) with an optional delta line (§6.4). */
export function Stat({
  label,
  value,
  delta,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  delta?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("flex min-w-0 flex-col", className)}>
      <span className="text-2xs text-fg-2">{label}</span>
      <span className="num text-lg font-medium text-fg-0">{value}</span>
      {delta !== undefined && <span className="text-xs">{delta}</span>}
    </div>
  );
}
