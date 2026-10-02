// 6. Experiment (§6.5): the paired books side by side, their equity against NIFTY, the daily
// reports and the pre-registration they are judged against.

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { get } from "../api/client";
import { keys } from "../api/queries";
import { num, pct } from "../lib/format";
import { Chart, type ChartLine } from "../ui/Chart";
import { Dialog } from "../ui/Dialog";
import { DateIST, Money, Pct } from "../ui/Num";
import { Empty, Panel, ToolButton } from "../ui/Panel";

interface ReportBook {
  risk_adjusted_mtd?: { sharpe?: number | null; sharpe_ci95?: [number, number] | null; sortino?: number | null; max_drawdown_pct?: number | null };
  trades?: { month_to_date?: { count?: number; win_rate?: number; expectancy_inr?: number | null } };
  costs?: { month_to_date?: { total?: number | null } };
}

function Markdown({ text }: { text: string }) {
  return <pre className="whitespace-pre-wrap font-mono text-xs leading-5 text-fg-0">{text}</pre>;
}

export default function Experiment() {
  const books = useQuery({ queryKey: keys.books, queryFn: ({ signal }) => get("/api/books", { signal }), refetchInterval: 30_000 });
  const equity = useQuery({ queryKey: ["equity"], queryFn: ({ signal }) => get("/api/equity", { signal }) });
  const reports = useQuery({ queryKey: ["reports"], queryFn: ({ signal }) => get("/api/reports", { query: { limit: 90 }, signal }) });
  const latest = reports.data?.[0]?.date;
  const report = useQuery({
    queryKey: keys.report(latest ?? ""),
    queryFn: ({ signal }) => get("/api/reports/{day}", { params: { day: latest! }, signal }),
    enabled: !!latest,
  });
  const [open, setOpen] = useState<string | null>(null); // a report date, or "prereg"
  const doc = useQuery({
    queryKey: ["doc", open],
    queryFn: ({ signal }) =>
      open === "prereg"
        ? get("/api/docs/preregistration", { signal })
        : get("/api/reports/{day}/markdown", { params: { day: open! }, signal }),
    enabled: open !== null,
  });

  const reportBooks = (report.data?.books ?? {}) as Record<string, ReportBook>;
  const lines = useMemo(() => {
    const tones = { A: "neutral", B: "accent", C: "warn" } as const;
    const out: ChartLine[] = (equity.data?.books ?? []).map((b) => ({
      name: `Book ${b.book_id}`,
      tone: tones[b.book_id as keyof typeof tones] ?? "neutral",
      points: b.points.map((p) => ({ time: p.date, value: p.return_pct })),
    }));
    if (equity.data?.benchmark_points.length) {
      out.push({ name: "NIFTY 50", tone: "neutral", dashed: true, points: equity.data.benchmark_points.map((p) => ({ time: p.date, value: p.return_pct })) });
    }
    return out;
  }, [equity.data]);

  return (
    <div className="flex flex-col gap-2 p-2">
      <Panel
        title={`Paired books · ${books.data?.experiment ?? ""}`}
        actions={
          <>
            {latest && <span className="text-2xs text-fg-2">risk statistics as of the {latest} report</span>}
            <ToolButton onClick={() => setOpen("prereg")}>Pre-registration</ToolButton>
          </>
        }
      >
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-line-strong text-2xs text-fg-2">
              {["Book", "Advisor", "Return", "Sharpe (95% CI)", "Sortino", "Max DD", "Trades", "Win %", "Expectancy", "Costs MTD", "AI spend", "Net AI value"].map((h, i) => (
                <th key={h} className={`px-2 py-0.5 font-normal ${i > 1 ? "text-right" : "text-left"}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(books.data?.books ?? []).map((b) => {
              const r = reportBooks[b.book_id];
              const ra = r?.risk_adjusted_mtd;
              const mtd = r?.trades?.month_to_date;
              return (
                <tr key={b.book_id} className="border-b">
                  <td className="px-2 py-0.5 font-medium">{b.book_id}</td>
                  <td className="px-2 py-0.5 text-fg-1">{b.advisor}</td>
                  <td className="px-2 py-0.5 text-right"><Pct value={b.return_pct} pnl /></td>
                  <td className="num px-2 py-0.5 text-right">
                    {ra?.sharpe == null ? "—" : num(ra.sharpe, 2)}
                    {ra?.sharpe_ci95 && <span className="text-fg-2"> ({num(ra.sharpe_ci95[0], 2)}, {num(ra.sharpe_ci95[1], 2)})</span>}
                  </td>
                  <td className="num px-2 py-0.5 text-right">{ra?.sortino == null ? "—" : num(ra.sortino, 2)}</td>
                  <td className="num px-2 py-0.5 text-right">{ra?.max_drawdown_pct == null ? "—" : pct(ra.max_drawdown_pct)}</td>
                  <td className="num px-2 py-0.5 text-right">{num(b.closed_trades, 0)}</td>
                  <td className="num px-2 py-0.5 text-right">{b.win_rate == null ? "—" : pct(b.win_rate * 100)}</td>
                  <td className="px-2 py-0.5 text-right"><Money value={mtd?.expectancy_inr} pnl /></td>
                  <td className="px-2 py-0.5 text-right"><Money value={r?.costs?.month_to_date?.total} /></td>
                  <td className="px-2 py-0.5 text-right"><Money value={b.ai_spend_inr_to_date} /></td>
                  <td className="px-2 py-0.5 text-right">{b.vs ? <Money value={b.net_ai_value_inr} pnl arrow /> : <span className="text-fg-2">baseline</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="px-2 py-1 text-2xs text-fg-2">
          Net AI value = (book equity − book A equity) − the book's AI spend. Return and net value are live; Sharpe, Sortino, drawdown, expectancy and costs come from the latest daily report.
        </p>
      </Panel>
      <Panel title="Return vs capital, % (NIFTY 50 dashed)">
        {lines.some((l) => l.points.length) ? (
          <Chart label="Each book's return on its capital and NIFTY 50's, percent, by session" lines={lines} height={260} axis="pct" />
        ) : (
          <Empty>No marks yet: the first is recorded when a session ends.</Empty>
        )}
      </Panel>
      <Panel title="Daily reports">
        {!reports.data?.length ? (
          <Empty>No daily report yet (written at 15:45 IST each session).</Empty>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-line-strong text-left text-2xs text-fg-2">
                <th className="px-2 py-0.5 font-normal">Session</th>
                {Object.keys(reports.data[0]!.books).map((b) => (
                  <th key={b} className="px-2 py-0.5 text-right font-normal">{`${b} day / net AI`}</th>
                ))}
                <th />
              </tr>
            </thead>
            <tbody>
              {reports.data.map((r) => (
                <tr key={r.date} className="border-b">
                  <td className="px-2 py-0.5"><DateIST value={r.date} /></td>
                  {Object.entries(r.books).map(([b, x]) => (
                    <td key={b} className="px-2 py-0.5 text-right">
                      <Pct value={x.day_return_pct} pnl /> {x.net_ai_value_inr != null && <> / <Money value={x.net_ai_value_inr} pnl /></>}
                    </td>
                  ))}
                  <td className="px-2 py-0.5 text-right">
                    <ToolButton onClick={() => setOpen(r.date)}>Open</ToolButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      <Dialog open={open !== null} onOpenChange={(o) => !o && setOpen(null)} title={doc.data?.title ?? "Loading…"} wide>
        {doc.data ? <Markdown text={doc.data.markdown} /> : <Empty>{doc.error ? "Could not load the document." : "Loading…"}</Empty>}
      </Dialog>
    </div>
  );
}
