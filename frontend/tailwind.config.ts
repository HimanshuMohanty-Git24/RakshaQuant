import type { Config } from "tailwindcss";

// The single source of the look. Colors resolve to the CSS variables declared in index.css,
// so density + palette can be retuned from one place. Names are semantic (up/down/cost),
// never raw hex, so nothing scatters hardcoded colors through the components.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "var(--canvas)",
        panel: "var(--panel)",
        elevated: "var(--elevated)",
        line: "var(--line)",
        ink: "var(--ink)",
        muted: "var(--muted)",
        amber: "var(--amber)",
        up: "var(--up)",
        down: "var(--down)",
        cost: "var(--cost)",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "IBM Plex Mono", "ui-monospace", "monospace"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "0.95rem" }], // 11px — dense data rows
        xs: ["0.75rem", { lineHeight: "1.05rem" }], // 12px
        sm: ["0.8125rem", { lineHeight: "1.15rem" }], // 13px
      },
      boxShadow: {
        drawer: "-16px 0 48px rgba(0,0,0,0.55)",
      },
      keyframes: {
        pulseDot: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        pulseDot: "pulseDot 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;
