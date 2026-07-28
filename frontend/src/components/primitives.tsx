import type { ReactNode } from "react";

// A titled panel with a hairline border and an amber-accented header — the base container
// for every region of the mission-control layout.
export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
  accent = false,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  accent?: boolean;
}) {
  return (
    <section
      className={`flex min-h-0 flex-col overflow-hidden rounded-md border bg-panel ${className}`}
    >
      <header className="flex items-center justify-between border-b bg-elevated/40 px-3 py-1.5">
        <h2
          className={`text-2xs font-medium uppercase tracking-[0.14em] ${
            accent ? "text-amber" : "text-muted"
          }`}
        >
          {title}
        </h2>
        {right}
      </header>
      <div className={`min-h-0 flex-1 overflow-auto ${bodyClassName}`}>{children}</div>
    </section>
  );
}

// A profit/loss value. Encodes direction with an arrow + sign AND color (never color alone).
export function Delta({
  value,
  format,
  className = "",
  showArrow = true,
}: {
  value: number;
  format: (v: number) => string;
  className?: string;
  showArrow?: boolean;
}) {
  const positive = value >= 0;
  const color = positive ? "text-up" : "text-down";
  const arrow = positive ? "▲" : "▼";
  return (
    <span className={`tnum ${color} ${className}`}>
      {showArrow && <span aria-hidden>{arrow} </span>}
      {format(value)}
    </span>
  );
}

// Long/short pill — label + color, so side is readable without relying on hue.
export function SideTag({ side }: { side: string }) {
  const isBuy = side.toUpperCase() === "BUY";
  return (
    <span
      className={`rounded-sm px-1.5 py-0.5 text-2xs font-medium ${
        isBuy ? "bg-up/15 text-up" : "bg-down/15 text-down"
      }`}
    >
      {isBuy ? "LONG" : "SHORT"}
    </span>
  );
}

export function Dot({ className = "", pulse = false }: { className?: string; pulse?: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${className} ${pulse ? "animate-pulseDot" : ""}`}
    />
  );
}

export function StatTile({
  label,
  value,
  sub,
  accent = "ink",
  spark,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: "ink" | "up" | "down" | "cost" | "amber";
  spark?: ReactNode;
}) {
  const color = {
    ink: "text-ink",
    up: "text-up",
    down: "text-down",
    cost: "text-cost",
    amber: "text-amber",
  }[accent];
  return (
    <div className="flex flex-col gap-0.5 border-b border-line/60 px-3 py-2 last:border-b-0">
      <span className="text-2xs uppercase tracking-wider text-muted">{label}</span>
      <div className="flex items-end justify-between gap-2">
        <span className={`tnum text-base font-medium leading-none ${color}`}>{value}</span>
        {spark}
      </div>
      {sub && <span className="tnum text-2xs text-muted">{sub}</span>}
    </div>
  );
}

// Dependency-free SVG sparkline. Trend color is amber by default (neutral accent).
export function Sparkline({
  data,
  width = 72,
  height = 22,
  stroke = "var(--amber)",
}: {
  data: number[];
  width?: number;
  height?: number;
  stroke?: string;
}) {
  if (data.length < 2) {
    return <svg width={width} height={height} aria-hidden />;
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const step = width / (data.length - 1);
  const points = data.map((d, i) => {
    const x = i * step;
    const y = height - 2 - ((d - min) / range) * (height - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={width} height={height} aria-hidden className="overflow-visible">
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={stroke}
        strokeWidth={1.25}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

// A thin labelled progress meter (win-rate, goal pace, budget). ``pct`` is 0..1.
export function Meter({
  pct: fraction,
  color = "var(--amber)",
  height = 6,
}: {
  pct: number;
  color?: string;
  height?: number;
}) {
  const clamped = Math.max(0, Math.min(1, fraction));
  return (
    <div className="w-full rounded-full bg-line/70" style={{ height }}>
      <div
        className="h-full rounded-full transition-[width] duration-500"
        style={{ width: `${clamped * 100}%`, background: color }}
      />
    </div>
  );
}
