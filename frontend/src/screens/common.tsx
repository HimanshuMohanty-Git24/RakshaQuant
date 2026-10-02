// Small pieces shared by the screens.

import type { ReactNode } from "react";
import { Link } from "react-router";

import { useSummary } from "../api/queries";
import { cx } from "../lib/cx";
import { dateIST, shortId } from "../lib/format";
import { sessionDay } from "../shell/TopBar";

/** The session's IST date (the demo's is its tape's day). */
export function useSessionDay(): string {
  const { data } = useSummary();
  return data?.session?.date ?? sessionDay(data?.now);
}

export function useBookIds(): string[] {
  const { data } = useSummary();
  return data?.books.map((b) => b.book_id) ?? [];
}

/** A segmented book chooser (A · B · C), optionally with "All". */
export function BookPicker({
  value,
  onChange,
  all = false,
}: {
  value: string | undefined;
  onChange: (book: string | undefined) => void;
  all?: boolean;
}) {
  const books = useBookIds();
  const options: { id: string | undefined; label: string }[] = [
    ...(all ? [{ id: undefined, label: "All" }] : []),
    ...books.map((b) => ({ id: b, label: b })),
  ];
  return (
    <div role="radiogroup" aria-label="Book" className="flex">
      {options.map((o) => (
        <button
          key={o.label}
          type="button"
          role="radio"
          aria-checked={value === o.id}
          onClick={() => onChange(o.id)}
          className={cx(
            "h-6 min-w-6 border-y border-l border-line-strong px-1.5 text-2xs first:rounded-l-sm last:rounded-r-sm last:border-r",
            value === o.id ? "bg-bg-2 text-fg-0" : "text-fg-2 hover:text-fg-0",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function DecisionLink({ id, children }: { id: string | null | undefined; children?: ReactNode }) {
  if (!id) return <span className="text-fg-2">—</span>;
  return (
    <Link to={`/decisions/${id}`} className="inline-flex min-h-6 min-w-6 items-center font-mono text-accent hover:underline" title={`Decision ${id}`}>
      {children ?? shortId(id)}
    </Link>
  );
}

export function SymbolLink({ symbol }: { symbol: string }) {
  return (
    <Link to={`/market?symbol=${encodeURIComponent(symbol)}`} className="inline-flex min-h-6 min-w-6 items-center text-fg-0 hover:text-accent hover:underline">
      {symbol}
    </Link>
  );
}

/** A labelled select for filter bars. */
export function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1 text-2xs text-fg-2">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-6 rounded-sm border border-line-strong bg-bg-0 px-1 text-xs text-fg-0"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** A date picker that also prints the date unambiguously (the native control follows the
 *  operating system's locale: 05/10 could be either month). */
export function DateFilter({ value, onChange }: { value: string; onChange: (value: string | null) => void }) {
  return (
    <label className="flex items-center gap-1 text-2xs text-fg-2">
      Date
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value || null)}
        className="h-6 rounded-sm border border-line-strong bg-bg-0 px-1 text-xs text-fg-0"
      />
      <span className="text-xs text-fg-1">{dateIST(value)} IST</span>
    </label>
  );
}

export function TextFilter({
  label,
  value,
  onChange,
  placeholder,
  width = "w-24",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  width?: string;
}) {
  return (
    <label className="flex items-center gap-1 text-2xs text-fg-2">
      {label}
      <input
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={cx("h-6 rounded-sm border border-line-strong bg-bg-0 px-1.5 text-xs text-fg-0 placeholder:text-fg-2", width)}
      />
    </label>
  );
}

/** Human labels for dispositions (what a book did with a signal). */
export const DISPOSITION: Record<string, string> = {
  submitted: "✓",
  vetoed: "VETO",
  risk_rejected: "risk ✕",
  broker_rejected: "broker ✕",
  regime_gated: "regime",
  policy_skipped: "skipped",
  shadow_strategy: "shadow",
  unknown: "unknown",
};
