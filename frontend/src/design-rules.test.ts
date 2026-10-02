// Plan §6.2/§6.7: the banned patterns, checked on every test run rather than by eye.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname);
const BANNED: [string, RegExp][] = [
  ["gradients", /gradient/i],
  ["glass / blur", /backdrop-blur|backdrop-filter/],
  ["drop shadows beyond the popover", /shadow-(sm|md|lg|xl|2xl|inner)\b/],
  ["radius above 4 px", /rounded-(md|lg|xl|2xl|3xl|full)\b/],
  ["bouncy motion", /animate-(bounce|ping|spin|pulse)\b/],
  ["emoji", /\p{Extended_Pictographic}/u],
  ["'AI-powered' copy", /ai[- ]powered|✨/i],
  ["purple/indigo palettes", /\b(purple|indigo|violet|fuchsia)-\d/],
];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    const own = /\.(tsx?|css)$/.test(name) && !/\.test\.tsx?$/.test(name) && !name.endsWith(".gen.ts");
    return own ? [path] : [];
  });
}

describe("design rules (§6.2)", () => {
  const files = sources(ROOT);

  it("finds the sources", () => {
    expect(files.length).toBeGreaterThan(10);
  });

  it.each(BANNED)("no %s anywhere", (_, pattern) => {
    const offenders = files.filter((file) => pattern.test(readFileSync(file, "utf8")));
    expect(offenders).toEqual([]);
  });
});
