import { lazy, Suspense } from "react";

import type { ChartProps } from "./ChartImpl";

export type { ChartLine, ChartMarker, ChartProps } from "./ChartImpl";

// lightweight-charts is loaded only when a chart is on screen: it stays out of the initial
// route's budget (§6.7).
const ChartImpl = lazy(() => import("./ChartImpl"));

export function Chart(props: ChartProps) {
  return (
    <Suspense fallback={<div style={{ height: props.height ?? 280 }} />}>
      <ChartImpl {...props} />
    </Suspense>
  );
}
