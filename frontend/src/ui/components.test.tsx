import { createColumnHelper } from "@tanstack/react-table";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Badge } from "./Badge";
import { DataTable, toCsv, type Columns } from "./DataTable";
import { ConfirmTyped } from "./Dialog";
import { Meter, meterTone } from "./Meter";
import { Money, Pct } from "./Num";

describe("formatter components", () => {
  it("signs and colours P&L, with an optional arrow", () => {
    const { container } = render(
      <>
        <Money value="1820" pnl arrow />
        <Money value="-710.5" pnl />
        <Money value="1004210" />
        <Pct value={-0.5} pnl />
      </>,
    );
    const spans = container.querySelectorAll(":scope > span");
    expect(spans[0]!.textContent).toBe("▲+₹1,820.00");
    expect(spans[0]!.className).toContain("text-up");
    expect(spans[1]!.textContent).toBe("−₹710.50");
    expect(spans[1]!.className).toContain("text-down");
    expect(spans[2]!.textContent).toBe("₹10,04,210.00");
    expect(spans[2]!.className).not.toContain("text-up");
    expect(spans[3]!.textContent).toBe("−0.50%");
  });
});

describe("Meter", () => {
  it("turns amber at 70% and red at 90%, and always writes the percentage", () => {
    expect([0.5, 0.7, 0.89, 0.9, null].map(meterTone)).toEqual(["ok", "warn", "warn", "crit", "ok"]);
    render(<Meter label="Daily loss" fraction={0.41} detail="₹4.1k / ₹10k" />);
    const meter = screen.getByRole("meter", { name: "Daily loss" });
    expect(meter.getAttribute("aria-valuenow")).toBe("41");
    expect(screen.getByText("41%")).toBeTruthy();
  });
});

describe("Badge", () => {
  it("maps status words to semantic tones and keeps the word", () => {
    render(
      <>
        <Badge>VETO</Badge>
        <Badge>BUY</Badge>
        <Badge>FLATTEN</Badge>
        <Badge>FILLED</Badge>
      </>,
    );
    expect(screen.getByText("VETO").className).toContain("text-warn");
    expect(screen.getByText("BUY").className).toContain("text-up");
    expect(screen.getByText("FLATTEN").className).toContain("bg-crit-bg");
    expect(screen.getByText("FILLED").className).toContain("text-fg-1"); // not green: not P&L
  });
});

describe("ConfirmTyped", () => {
  it("arms only on the exact phrase and a reason, then reports server errors", async () => {
    const onConfirm = vi.fn().mockRejectedValueOnce(new Error("a HALT file is present")).mockResolvedValue({});
    render(
      <ConfirmTyped
        open
        onOpenChange={() => {}}
        title="Resume book A"
        description="Re-arms the global kill switch."
        phrase="RESUME"
        confirmLabel="Resume"
        onConfirm={onConfirm}
      />,
    );
    const button = screen.getByRole("button", { name: "Resume" }) as HTMLButtonElement;
    const [reason, phrase] = screen.getAllByRole("textbox");
    fireEvent.change(reason!, { target: { value: "checked the feed" } });
    fireEvent.change(phrase!, { target: { value: "resume" } });
    expect(button.disabled).toBe(true); // case matters
    fireEvent.change(phrase!, { target: { value: "RESUME" } });
    expect(button.disabled).toBe(false);
    await act(async () => fireEvent.click(button));
    expect(onConfirm).toHaveBeenCalledWith("checked the feed");
    expect(screen.getByRole("alert").textContent).toBe("a HALT file is present");
  });
});

interface Row {
  id: string;
  symbol: string;
  pnl: string;
}
const helper = createColumnHelper<Row>();
const columns: Columns<Row> = [
  helper.accessor("symbol", { header: "Symbol" }),
  helper.accessor("pnl", { header: "Net P&L", meta: { align: "right" }, cell: (c) => <Money value={c.getValue()} pnl /> }),
];

describe("DataTable", () => {
  // jsdom has no layout: give every element a 800x400 box so the virtualiser has a viewport.
  const size = (name: "offsetHeight" | "offsetWidth", value: number) =>
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  beforeEach(() => {
    size("offsetHeight", 400);
    size("offsetWidth", 800);
  });
  afterEach(() => {
    size("offsetHeight", 0);
    size("offsetWidth", 0);
  });

  it("virtualises thousands of rows, sorts, and opens a row", () => {
    const data = Array.from({ length: 5000 }, (_, i) => ({ id: `t${i}`, symbol: `S${i}`, pnl: String(i - 2500) }));
    const onRowClick = vi.fn();
    render(<DataTable label="Trades" data={data} columns={columns} getRowId={(r) => r.id} onRowClick={onRowClick} />);
    const rows = screen.getAllByRole("row");
    expect(rows.length).toBeGreaterThan(5);
    expect(rows.length).toBeLessThan(60); // only what is visible (+ overscan) is in the DOM
    expect(screen.getByRole("table").getAttribute("aria-rowcount")).toBe("5001");
    fireEvent.click(screen.getByRole("button", { name: /Net P&L/ }));
    expect(screen.getByRole("columnheader", { name: /Net P&L/ }).getAttribute("aria-sort")).toBe("ascending");
    fireEvent.keyDown(screen.getAllByRole("row")[1]!, { key: "Enter" });
    expect(onRowClick).toHaveBeenCalledWith(expect.objectContaining({ pnl: "-2500" }));
  });

  it("says why it is empty, and surfaces errors", () => {
    const { rerender } = render(<DataTable label="T" data={[]} columns={columns} empty="No trades today." />);
    expect(screen.getByText("No trades today.")).toBeTruthy();
    rerender(<DataTable label="T" data={[]} columns={columns} error={new Error("401 unauthorized")} />);
    expect(screen.getByText("Could not load: 401 unauthorized")).toBeTruthy();
  });

  it("exports raw values as CSV", () => {
    const csv = toCsv([{ id: "1", symbol: 'M&M "A"', pnl: "-1.50" }], columns);
    expect(csv).toBe('Symbol,Net P&L\n"M&M ""A""",-1.50\n');
  });
});
