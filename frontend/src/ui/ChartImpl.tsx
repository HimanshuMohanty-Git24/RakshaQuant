// lightweight-charts behind a small declarative API: candles (+ volume), up to three lines,
// and markers for entries, exits and events (§6.4). Loaded lazily through ./Chart.

import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

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
}

export interface ChartMarker {
  time: string;
  kind: "entry" | "exit" | "event";
  text: string;
}

export interface ChartProps {
  candles?: Candle[];
  lines?: ChartLine[];
  markers?: ChartMarker[];
  height?: number;
  label: string; // the chart's accessible name, e.g. "INFY daily candles, ₹"
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

export default function ChartImpl({ candles, lines = [], markers = [], height = 280, label }: ChartProps) {
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
      timeScale: { borderColor: grid },
      crosshair: { mode: 0 },
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
    for (const line of lines.slice(0, 3)) {
      const series = api.addSeries(LineSeries, { color: tones[line.tone ?? "accent"], lineWidth: 2 });
      series.setData(line.points.map((p) => ({ time: p.time as Time, value: p.value })));
    }
    api.timeScale().fitContent();
    return () => {
      api.remove();
      chart.current = null;
    };
  }, [candles, lines, markers, height]);

  return <div ref={host} role="img" aria-label={label} style={{ height }} />;
}
