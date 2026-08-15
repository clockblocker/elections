export type ChartView = { x0: number; x1: number; y0: number; y1: number };

export const FULL_CHART_VIEW: ChartView = { x0: 0, x1: 100, y0: 0, y1: 100 };

const DOMAIN_MIN = 0;
const DOMAIN_MAX = 100;
const MIN_SPAN = 0.01;

function boundAxis(start: number, end: number): [number, number] {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return [DOMAIN_MIN, DOMAIN_MAX];
  const midpoint = (start + end) / 2;
  const requestedSpan = Math.abs(end - start);
  if (requestedSpan >= DOMAIN_MAX - DOMAIN_MIN) return [DOMAIN_MIN, DOMAIN_MAX];

  const span = Math.max(MIN_SPAN, requestedSpan);
  let low = midpoint - span / 2;
  let high = midpoint + span / 2;
  if (low < DOMAIN_MIN) { high += DOMAIN_MIN - low; low = DOMAIN_MIN; }
  if (high > DOMAIN_MAX) { low -= high - DOMAIN_MAX; high = DOMAIN_MAX; }
  return [Math.max(DOMAIN_MIN, low), Math.min(DOMAIN_MAX, high)];
}

export function boundChartView(view: ChartView): ChartView {
  const [x0, x1] = boundAxis(view.x0, view.x1);
  const [y0, y1] = boundAxis(view.y0, view.y1);
  return { x0, x1, y0, y1 };
}
