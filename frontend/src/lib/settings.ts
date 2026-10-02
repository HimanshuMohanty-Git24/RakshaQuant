// Operator preferences, kept in localStorage. Single-key shortcuts are OFF by default
// (WCAG 2.1.4, plan §6.1): Ctrl/⌘+K always works.

import { create } from "zustand";
import { persist } from "zustand/middleware";

interface Settings {
  shortcuts: boolean;
  navExpanded: boolean;
  toggleShortcuts: () => void;
  toggleNav: () => void;
}

export const useSettings = create<Settings>()(
  persist(
    (set, get) => ({
      shortcuts: false,
      navExpanded: true,
      toggleShortcuts: () => set({ shortcuts: !get().shortcuts }),
      toggleNav: () => set({ navExpanded: !get().navExpanded }),
    }),
    { name: "rq.settings" },
  ),
);
