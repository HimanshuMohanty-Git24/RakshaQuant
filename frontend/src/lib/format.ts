// Number and time formatting (plan §6.1). The server sends exact decimals as strings; these
// functions only *format* them - the UI never computes money (§6.6).

export type Numeric = number | string | null | undefined;
export type Direction = "up" | "down" | "flat";

const MINUS = "−"; // a real minus sign, not a hyphen
const IST = "Asia/Kolkata";

const grouped = (decimals: number) =>
  new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
const GROUPED: Record<number, Intl.NumberFormat> = {};
function group(value: number, decimals: number): string {
  GROUPED[decimals] ??= grouped(decimals);
  return GROUPED[decimals]!.format(value);
}

export function toNumber(value: Numeric): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function direction(value: Numeric): Direction {
  const n = toNumber(value);
  if (n === null || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function signed(text: string, n: number, sign: boolean): string {
  if (n < 0) return `${MINUS}${text}`;
  return sign && n > 0 ? `+${text}` : text;
}

/** ₹1,23,456.78 - Indian digit grouping; ``sign`` adds + for gains (losses always show −). */
export function inr(value: Numeric, { sign = false, decimals = 2 } = {}): string {
  const n = toNumber(value);
  if (n === null) return "—";
  return signed(`₹${group(Math.abs(n), decimals)}`, n, sign);
}

/** Compact INR for tight cells: ₹4.1k, ₹10.04L (lakh), ₹1.20Cr (crore). */
export function inrCompact(value: Numeric, { sign = false } = {}): string {
  const n = toNumber(value);
  if (n === null) return "—";
  const a = Math.abs(n);
  const text =
    a >= 1e7
      ? `₹${(a / 1e7).toFixed(2)}Cr`
      : a >= 1e5
        ? `₹${(a / 1e5).toFixed(2)}L`
        : a >= 1e3
          ? `₹${(a / 1e3).toFixed(1)}k`
          : `₹${a.toFixed(0)}`;
  return signed(text, n, sign);
}

/** A price: 2 dp, Indian grouping, no symbol. */
export function price(value: Numeric, decimals = 2): string {
  const n = toNumber(value);
  return n === null ? "—" : signed(group(Math.abs(n), decimals), n, false);
}

/** A percentage given in percent units (1.5 → "1.50%"). */
export function pct(value: Numeric, { sign = false, decimals = 2 } = {}): string {
  const n = toNumber(value);
  return n === null ? "—" : signed(`${Math.abs(n).toFixed(decimals)}%`, n, sign);
}

/** A ratio given as a fraction (0.015 → "1.50%"). */
export function ratio(value: Numeric, { sign = false, decimals = 2 } = {}): string {
  const n = toNumber(value);
  return n === null ? "—" : pct(n * 100, { sign, decimals });
}

/** Basis points, 1 dp. */
export function bps(value: Numeric, { sign = false } = {}): string {
  const n = toNumber(value);
  return n === null ? "—" : signed(`${Math.abs(n).toFixed(1)} bps`, n, sign);
}

/** A quantity: an integer with Indian grouping. */
export function qty(value: Numeric): string {
  const n = toNumber(value);
  return n === null ? "—" : signed(group(Math.abs(Math.round(n)), 0), n, false);
}

/** A plain number with fixed decimals. */
export function num(value: Numeric, decimals = 2): string {
  const n = toNumber(value);
  return n === null ? "—" : signed(group(Math.abs(n), decimals), n, false);
}

// -- time (IST everywhere, §6.1) -------------------------------------------------------------

const TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
const HHMM = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const DATE = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  weekday: "short",
  day: "2-digit",
  month: "short",
});

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 09:42:13 (IST). */
export function timeIST(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  return d ? TIME.format(d) : "—";
}

/** 09:42 (IST). */
export function hhmmIST(value: string | number | Date | null | undefined): string {
  const d = toDate(value);
  return d ? HHMM.format(d) : "—";
}

/** Mon 05 Oct (IST). A bare ``YYYY-MM-DD`` is a calendar date, not a UTC midnight. */
export function dateIST(value: string | number | Date | null | undefined): string {
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return DATE.format(new Date(`${value}T12:00:00+05:30`)).replace(",", "");
  }
  const d = toDate(value);
  return d ? DATE.format(d).replace(",", "") : "—";
}

/** "12 s ago" - a secondary hint only (§6.1). */
export function relative(value: string | number | Date | null | undefined, now = Date.now()): string {
  const d = toDate(value);
  if (!d) return "—";
  const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/** A duration in seconds: 930 → "15m 30s". */
export function duration(seconds: Numeric): string {
  const n = toNumber(seconds);
  if (n === null) return "—";
  const s = Math.round(n);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** The symbol of an instrument key (``NSE:EQ:INFY`` → ``INFY``). */
export function symbolOf(instrumentKey: string): string {
  return instrumentKey.split(":").pop() ?? instrumentKey;
}

/** A short id for dense cells: the last 8 characters. */
export function shortId(id: string | null | undefined): string {
  return id ? (id.length > 10 ? `…${id.slice(-8)}` : id) : "—";
}
