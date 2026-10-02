// Toasts for alerts and control outcomes (§6.4), announced through an aria-live region.

import { create } from "zustand";

import { cx } from "../lib/cx";

export type ToastLevel = "info" | "warn" | "crit";

export interface ToastItem {
  id: number;
  level: ToastLevel;
  title: string;
  body?: string;
}

interface ToastState {
  toasts: ToastItem[];
  push: (toast: Omit<ToastItem, "id">) => void;
  dismiss: (id: number) => void;
}

let next = 1;

export const useToasts = create<ToastState>((set, get) => ({
  toasts: [],
  push: (toast) => {
    const id = next++;
    set({ toasts: [...get().toasts.slice(-4), { ...toast, id }] });
    if (toast.level === "info") setTimeout(() => get().dismiss(id), 6_000);
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

const LEVEL = {
  info: "border-line-strong",
  warn: "border-warn",
  crit: "border-crit-bg bg-crit-bg text-crit-fg",
} as const;

export function Toaster() {
  const { toasts, dismiss } = useToasts();
  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed bottom-8 right-3 z-50 flex w-[360px] flex-col gap-2"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cx("pointer-events-auto rounded border bg-bg-1 px-3 py-2 shadow-popover", LEVEL[t.level])}
        >
          <div className="flex items-start justify-between gap-2">
            <p className="text-xs font-medium">{t.title}</p>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => dismiss(t.id)}
              className="text-xs text-fg-2 hover:text-fg-0"
            >
              ×
            </button>
          </div>
          {t.body && <p className="mt-0.5 text-xs text-fg-1">{t.body}</p>}
        </div>
      ))}
    </div>
  );
}
