// Formatter components (§6.4): every number on screen goes through one of these, so decimals,
// signs, grouping and IST are consistent everywhere. P&L shows a sign *and* a colour (and an
// arrow on request) - colour is never the only signal.

import { cx } from "../lib/cx";
import {
  bps,
  dateIST,
  direction,
  inr,
  inrCompact,
  num,
  pct,
  price,
  qty,
  relative,
  timeIST,
  type Numeric,
} from "../lib/format";

const TONE = { up: "text-up", down: "text-down", flat: "text-fg-1" } as const;
const ARROW = { up: "▲", down: "▼", flat: "" } as const;

interface Signed {
  value: Numeric;
  /** Colour by direction and always sign: a P&L, a return, an excess. */
  pnl?: boolean;
  arrow?: boolean;
  className?: string;
}

function Directional({ value, pnl, arrow, className, text }: Signed & { text: string }) {
  const dir = direction(value);
  return (
    <span className={cx("num whitespace-nowrap", pnl && TONE[dir], className)}>
      {pnl && arrow && dir !== "flat" && (
        <span aria-hidden="true" className="mr-0.5 text-2xs">
          {ARROW[dir]}
        </span>
      )}
      {text}
    </span>
  );
}

export function Money({ value, pnl, arrow, className, compact }: Signed & { compact?: boolean }) {
  const text = compact ? inrCompact(value, { sign: pnl }) : inr(value, { sign: pnl });
  return <Directional value={value} pnl={pnl} arrow={arrow} className={className} text={text} />;
}

export function Pct({ value, pnl, arrow, className, decimals = 2 }: Signed & { decimals?: number }) {
  const text = pct(value, { sign: pnl, decimals });
  return <Directional value={value} pnl={pnl} arrow={arrow} className={className} text={text} />;
}

export function Bps({ value, pnl, className }: Signed) {
  return <Directional value={value} pnl={pnl} className={className} text={bps(value, { sign: pnl })} />;
}

export function Price({ value, className }: { value: Numeric; className?: string }) {
  return <span className={cx("num whitespace-nowrap", className)}>{price(value)}</span>;
}

export function Qty({ value, className }: { value: Numeric; className?: string }) {
  return <span className={cx("num whitespace-nowrap", className)}>{qty(value)}</span>;
}

export function Num({
  value,
  decimals = 2,
  className,
}: {
  value: Numeric;
  decimals?: number;
  className?: string;
}) {
  return <span className={cx("num whitespace-nowrap", className)}>{num(value, decimals)}</span>;
}

type When = string | number | Date | null | undefined;

/** A time in IST; hover shows the relative age (a secondary hint, §6.1). */
export function TimeIST({ value, label, className }: { value: When; label?: boolean; className?: string }) {
  if (value === null || value === undefined || value === "") {
    return <span className={className}>—</span>;
  }
  const iso = new Date(value).toISOString();
  return (
    <time dateTime={iso} title={relative(value)} className={cx("num whitespace-nowrap", className)}>
      {timeIST(value)}
      {label && <span className="ml-1 text-fg-2">IST</span>}
    </time>
  );
}

export function DateIST({ value, className }: { value: When; className?: string }) {
  return <span className={cx("num whitespace-nowrap", className)}>{dateIST(value)}</span>;
}
