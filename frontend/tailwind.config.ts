import type { Config } from "tailwindcss";

// The design tokens of plan §6.3, as semantic names over the CSS variables in index.css.
// Radii stop at 4 px and there is one shadow (popovers): the §6.2 bans are enforced by the
// theme itself, so `rounded-xl` or `shadow-lg` simply do not exist here.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      bg: { 0: "var(--bg-0)", 1: "var(--bg-1)", 2: "var(--bg-2)" },
      line: { DEFAULT: "var(--border)", strong: "var(--border-strong)" },
      fg: { 0: "var(--text-0)", 1: "var(--text-1)", 2: "var(--text-2)" },
      accent: "var(--accent)",
      up: "var(--up)",
      down: "var(--down)",
      warn: "var(--warn)",
      crit: { bg: "var(--crit-bg)", fg: "var(--crit-fg)" },
      overlay: "var(--overlay)", // behind dialogs only
    },
    borderRadius: { none: "0", sm: "var(--radius-sm)", DEFAULT: "var(--radius)" },
    boxShadow: { none: "none", popover: "0 4px 16px rgba(0, 0, 0, 0.45)" },
    fontFamily: { sans: "var(--font-ui)", mono: "var(--font-mono)" },
    fontSize: {
      "2xs": ["11px", { lineHeight: "16px" }], // labels
      xs: ["12px", { lineHeight: "18px" }], // dense tables
      sm: ["13px", { lineHeight: "20px" }], // base UI
      base: ["14px", { lineHeight: "20px" }],
      lg: ["16px", { lineHeight: "22px" }], // Stat values
      xl: ["20px", { lineHeight: "26px" }],
      "2xl": ["24px", { lineHeight: "30px" }], // the ceiling (§6.2)
    },
    extend: {
      spacing: { row: "24px", "row-lg": "28px" },
      transitionDuration: { DEFAULT: "100ms" },
    },
  },
  plugins: [],
} satisfies Config;
