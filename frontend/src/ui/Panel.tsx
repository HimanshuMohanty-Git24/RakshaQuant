import type { ReactNode } from "react";

import { cx } from "../lib/cx";

/** 1 px border, 4 px radius, a 28 px header with the title and actions (§6.4). */
export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  scrollable = false,
}: {
  title: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** A fixed-height panel whose body scrolls: make it reachable by keyboard. */
  scrollable?: boolean;
}) {
  return (
    <section className={cx("flex min-h-0 min-w-0 flex-col rounded border bg-bg-1", className)}>
      <header className="flex h-7 shrink-0 items-center justify-between gap-2 border-b px-2">
        <h2 className="truncate text-xs font-medium text-fg-1">{title}</h2>
        {actions && <div className="flex items-center gap-1">{actions}</div>}
      </header>
      <div
        className={cx("min-h-0 flex-1 overflow-auto", bodyClassName)}
        tabIndex={scrollable ? 0 : undefined}
        role={scrollable ? "region" : undefined}
        aria-label={scrollable && typeof title === "string" ? title : undefined}
      >
        {children}
      </div>
    </section>
  );
}

/** One calm line explaining why there is nothing to show (§6.1). */
export function Empty({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cx("px-2 py-3 text-xs text-fg-2", className)}>{children}</p>;
}

/** A small text button used in panel headers and toolbars. */
export function ToolButton({
  children,
  onClick,
  disabled,
  title,
  tone = "neutral",
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  tone?: "neutral" | "crit" | "accent";
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cx(
        "h-6 rounded-sm border px-2 text-xs disabled:cursor-not-allowed disabled:opacity-50",
        tone === "crit" && "border-crit-bg bg-crit-bg text-crit-fg hover:brightness-110",
        tone === "accent" && "border-accent text-accent hover:bg-bg-2",
        tone === "neutral" && "border-line-strong text-fg-1 hover:bg-bg-2 hover:text-fg-0",
      )}
    >
      {children}
    </button>
  );
}
