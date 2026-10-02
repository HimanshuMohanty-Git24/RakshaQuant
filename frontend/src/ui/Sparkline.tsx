/** A tiny SVG line for tiles: no axes, so it carries an accessible label instead (§6.4). */
export function Sparkline({
  values,
  label,
  width = 80,
  height = 20,
  className = "text-fg-1",
}: {
  values: number[];
  label: string;
  width?: number;
  height?: number;
  className?: string;
}) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (width - 2) + 1;
      const y = height - 1 - ((v - min) / span) * (height - 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg role="img" aria-label={label} width={width} height={height} className={className}>
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.25" />
    </svg>
  );
}
