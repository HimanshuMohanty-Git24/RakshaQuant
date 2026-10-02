// Plain 16 px line glyphs for the navigation: monochrome, no decoration.

const PATHS: Record<string, string> = {
  command: "M2 2h5v5H2zM9 2h5v5H9zM2 9h5v5H2zM9 9h5v5H9z",
  decisions: "M3 3h10M3 8h10M3 13h6M12 11l1.5 1.5L16 10",
  blotter: "M2 3h12v10H2zM2 6.5h12M2 9.5h12M6 3v10",
  risk: "M8 1.5l5.5 2v4.5c0 3.2-2.4 5.6-5.5 6.5-3.1-.9-5.5-3.3-5.5-6.5V3.5z",
  ai: "M4 4h8v8H4zM6 1.5V4M10 1.5V4M6 12v2.5M10 12v2.5M1.5 6H4M1.5 10H4M12 6h2.5M12 10h2.5",
  experiment: "M2 13.5h12M4 13.5V7M8 13.5V3M12 13.5V9",
  market: "M2 12l3.5-4 3 2.5L14 4M10.5 4H14v3.5",
  system: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11zM8 8l3-2.5",
};

export function Icon({ name }: { name: keyof typeof PATHS | string }) {
  return (
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d={PATHS[name] ?? ""} stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
    </svg>
  );
}
