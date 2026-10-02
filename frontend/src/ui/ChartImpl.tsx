// lightweight-charts behind a small declarative API: candles (+ volume), lines in at most three
// colours (a dashed benchmark may reuse one), and entry/exit/event markers (§6.4). Lazy (./Chart).

import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import { inrCompact, pct, price } from "../lib/format";

export interface Candle {
  time: string; // YYYY-MM-DD
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface ChartLine {
  name: string;
  points: { time: string; value: number }[];
  tone?: "accent" | "neutral" | "warn";
  dashed?: boolean; // a benchmark: same palette, different stroke
}

export interface ChartMarker {
  time: string;
  kind: "entry" | "exit" | "event";
  text: string;
}

export interface ChartProps {
  /** How the price axis reads: rupees (grouped en-IN), percent, or a plain number. */
  axis?: "inr" | "pct" | "price";
  candles?: Candle[];
  lines?: ChartLine[];
  markers?: ChartMarker[];
  height?: number;
  label: string; // the chart's accessible name, e.g. "INFY daily candles, ₹"
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Session dates arrive as business days; label them like the rest of the UI (05 Oct). */
function dayLabel(time: Time): string {
  if (typeof time === "object") return `${String(time.day).padStart(2, "0")} ${MONTHS[time.month - 1]}`;
  if (typeof time === "string") return dayLabel({ year: +time.slice(0, 4), month: +time.slice(5, 7), day: +time.slice(8, 10) });
  return new Date(time * 1000).toISOString().slice(0, 10);
}

export default function ChartImpl({ candles, lines = [], markers = [], height = 280, label, axis = "price" }: ChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const text = cssVar("--text-2");
    const grid = cssVar("--border");
    const api = createChart(host.current, {
      height,
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: text,
        fontFamily: cssVar("--font-mono"),
        fontSize: 11,
        attributionLogo: false,
      },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
      rightPriceScale: { borderColor: grid },
      timeScale: { borderColor: grid, tickMarkFormatter: (t: Time) => dayLabel(t) },
      crosshair: { mode: 0 },
      localization: {
        locale: "en-IN",
        priceFormatter: (v: number) => (axis === "pct" ? pct(v) : axis === "inr" ? inrCompact(v) : price(v)),
        timeFormatter: (t: Time) => dayLabel(t),
      },
    });
    chart.current = api;
    const up = cssVar("--up");
    const down = cssVar("--down");
    if (candles?.length) {
      const series = api.addSeries(CandlestickSeries, {
        upColor: up,
        downColor: down,
        borderUpColor: up,
        borderDownColor: down,
        wickUpColor: up,
        wickDownColor: down,
      });
      series.setData(candles.map((c) => ({ ...c, time: c.time as Time })));
      if (candles.some((c) => c.volume !== undefined)) {
        const volume = api.addSeries(HistogramSeries, {
          priceScaleId: "volume",
          priceFormat: { type: "volume" },
          color: cssVar("--border-strong"),
          lastValueVisible: false,
          priceLineVisible: false,
        });
        api.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
        volume.setData(candles.map((c) => ({ time: c.time as Time, value: c.volume ?? 0 })));
      }
      if (markers.length) {
        const ordered = [...markers].sort((a, b) => a.time.localeCompare(b.time));
        createSeriesMarkers(
          series,
          ordered.map(
            (m): SeriesMarker<Time> => ({
              time: m.time as Time,
              position: m.kind === "exit" ? "aboveBar" : "belowBar",
              shape: m.kind === "entry" ? "arrowUp" : m.kind === "exit" ? "arrowDown" : "circle",
              color: m.kind === "event" ? cssVar("--warn") : cssVar("--accent"),
              text: m.text,
            }),
          ),
        );
      }
    }
    const tones = { accent: cssVar("--accent"), neutral: cssVar("--text-1"), warn: cssVar("--warn") };
    for (const line of lines.slice(0, 4)) {
      const series = api.addSeries(LineSeries, {
        color: tones[line.tone ?? "accent"],
        lineWidth: 2,
        lineStyle: line.dashed ? LineStyle.Dashed : LineStyle.Solid,
        title: line.name,
      });
      series.setData(line.points.map((p) => ({ time: p.time as Time, value: p.value })));
    }
    api.timeScale().fitContent();
    return () => {
      api.remove();
      chart.current = null;
    };
  }, [candles, lines, markers, height, axis]);

  return <div ref={host} role="img" aria-label={label} style={{ height }} />;
}
