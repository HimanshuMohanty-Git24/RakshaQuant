// The blotter table (§6.4): TanStack Table + Virtual. Sticky header, sortable and resizable
// columns, 24 px rows, row → inspector (click or Enter), CSV export, calm empty/error states.
// Only the visible rows are in the DOM, so 5,000 rows scroll like 50 (§6.7).

import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type RowData,
  type SortingFn,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef, useState, type ReactNode } from "react";

import { cx } from "../lib/cx";
import { toNumber, type Numeric } from "../lib/format";
import { Empty } from "./Panel";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    align?: "left" | "right";
    /** The raw value for CSV export (defaults to the accessor's value). */
    csv?: (row: TData) => string | number | boolean | null | undefined;
  }
}

const ROW = 24;

/** Money and prices arrive as decimal strings: compare them as numbers, everything else
 *  (ids, ISO timestamps) as text. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const smartSort: SortingFn<any> = (a, b, id) => {
  const x = a.getValue(id);
  const y = b.getValue(id);
  const nx = toNumber(x as Numeric);
  const ny = toNumber(y as Numeric);
  if (nx !== null && ny !== null) return nx === ny ? 0 : nx < ny ? -1 : 1;
  return String(x ?? "").localeCompare(String(y ?? ""));
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Columns<T> = ColumnDef<T, any>[];

export interface DataTableProps<T> {
  data: T[] | undefined;
  columns: Columns<T>;
  label: string;
  getRowId?: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  rowLabel?: (row: T) => string;
  loading?: boolean;
  error?: unknown;
  empty?: ReactNode;
  initialSorting?: SortingState;
  className?: string;
}

export function DataTable<T>({
  data,
  columns,
  label,
  getRowId,
  onRowClick,
  rowLabel,
  loading,
  error,
  empty,
  initialSorting = [],
  className,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: data ?? [],
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getRowId,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    columnResizeMode: "onChange",
    defaultColumn: { size: 110, minSize: 48, maxSize: 600, sortingFn: smartSort },
  });
  const scroller = useRef<HTMLDivElement>(null);
  const rows = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW,
    overscan: 12,
  });
  const width = table.getTotalSize();

  if (error) {
    const message = error instanceof Error ? error.message : "request failed";
    return <Empty className="text-warn">Could not load: {message}</Empty>;
  }

  return (
    <div
      ref={scroller}
      role="table"
      aria-label={label}
      tabIndex={onRowClick ? undefined : 0} // rows that open nothing: the table itself scrolls by keyboard
      aria-rowcount={rows.length + 1}
      className={cx("relative h-full min-h-0 overflow-auto text-xs", className)}
    >
      <div style={{ minWidth: width }}>
        <div role="rowgroup" className="sticky top-0 z-10 bg-bg-2">
          {table.getHeaderGroups().map((group) => (
            <div role="row" key={group.id} className="flex h-6 border-b border-line-strong">
              {group.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                const align = header.column.columnDef.meta?.align ?? "left";
                return (
                  <div
                    role="columnheader"
                    key={header.id}
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}
                    className="relative flex shrink-0 items-center px-2 text-2xs text-fg-2"
                    style={{ width: header.getSize() }}
                  >
                    {header.isPlaceholder ? null : header.column.getCanSort() ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className={cx("flex w-full items-center gap-1 hover:text-fg-0", align === "right" && "justify-end")}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        <span aria-hidden="true">{sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : ""}</span>
                      </button>
                    ) : (
                      <span className={cx("w-full", align === "right" && "text-right")}>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                      </span>
                    )}
                    {header.column.getCanResize() && (
                      <span
                        role="separator"
                        aria-orientation="vertical"
                        aria-label="Resize column"
                        onMouseDown={header.getResizeHandler()}
                        onTouchStart={header.getResizeHandler()}
                        className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-accent"
                      />
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        {rows.length === 0 ? (
          <Empty>{loading ? "Loading…" : (empty ?? "Nothing to show.")}</Empty>
        ) : (
          <div role="rowgroup" className="relative" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((item) => {
              const row = rows[item.index]!;
              const clickable = onRowClick !== undefined;
              return (
                <div
                  role="row"
                  key={row.id}
                  aria-rowindex={item.index + 2}
                  aria-label={rowLabel?.(row.original)}
                  tabIndex={clickable ? 0 : undefined}
                  onClick={clickable ? () => onRowClick(row.original) : undefined}
                  onKeyDown={
                    clickable
                      ? (e) => {
                          if (e.key === "Enter") onRowClick(row.original);
                        }
                      : undefined
                  }
                  className={cx(
                    "absolute left-0 flex w-full items-center border-b border-line",
                    clickable && "cursor-pointer hover:bg-bg-2 focus-visible:bg-bg-2",
                  )}
                  style={{ height: ROW, transform: `translateY(${item.start}px)` }}
                >
                  {row.getVisibleCells().map((cell) => (
                    <div
                      role="cell"
                      key={cell.id}
                      className={cx(
                        "shrink-0 truncate px-2 text-fg-0",
                        cell.column.columnDef.meta?.align === "right" && "text-right",
                      )}
                      style={{ width: cell.column.getSize() }}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

// -- CSV export ---------------------------------------------------------------------------------

function csvValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** The rows as CSV, one column per table column (raw values, not formatted text). */
export function toCsv<T>(rows: T[], columns: Columns<T>): string {
  const usable = columns.filter((c) => "accessorKey" in c || "accessorFn" in c || c.meta?.csv);
  const header = usable.map((c) => csvValue(typeof c.header === "string" ? c.header : c.id));
  const lines = rows.map((row, index) =>
    usable
      .map((c) => {
        if (c.meta?.csv) return csvValue(c.meta.csv(row));
        if ("accessorFn" in c && c.accessorFn) return csvValue(c.accessorFn(row, index));
        if ("accessorKey" in c) return csvValue((row as Record<string, unknown>)[String(c.accessorKey)]);
        return "";
      })
      .join(","),
  );
  return [header.join(","), ...lines].join("\n") + "\n";
}

export function downloadCsv<T>(name: string, rows: T[], columns: Columns<T>): void {
  const blob = new Blob([toCsv(rows, columns)], { type: "text/csv;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = name.endsWith(".csv") ? name : `${name}.csv`;
  a.click();
  URL.revokeObjectURL(href);
}
