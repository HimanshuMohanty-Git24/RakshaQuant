// Number / currency / percent formatting. All money is INR (₹) and all numbers render with
// tabular figures (the .tnum class + font-feature-settings) so columns align.

export function inr(value: number, digits = 2): string {
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  return `${sign}₹${abs.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function inrCompact(value: number): string {
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)}Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)}L`;
  if (abs >= 1e3) return `${sign}₹${(abs / 1e3).toFixed(1)}k`;
  return `${sign}₹${abs.toFixed(0)}`;
}

export function signed(value: number, digits = 2): string {
  const s = value >= 0 ? "+" : "−";
  return `${s}${Math.abs(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function signedInr(value: number, digits = 2): string {
  const s = value >= 0 ? "+" : "−";
  return `${s}₹${Math.abs(value).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function pct(value: number, digits = 2): string {
  const s = value >= 0 ? "+" : "−";
  return `${s}${Math.abs(value).toFixed(digits)}%`;
}

export function pctPlain(value01: number): string {
  return `${Math.round(value01 * 100)}%`;
}

export function usd(value: number, digits = 4): string {
  return `$${value.toFixed(digits)}`;
}

export function num(value: number): string {
  return value.toLocaleString("en-US");
}

export function ms(value: number | null): string {
  if (value == null) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  return `${value}ms`;
}

export function clockTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
  } catch {
    return iso;
  }
}

// p50 / p99 of an array of numbers (nearest-rank). Returns null for empty input.
export function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.ceil((p / 100) * sorted.length);
  return sorted[Math.min(sorted.length - 1, Math.max(0, rank - 1))];
}
