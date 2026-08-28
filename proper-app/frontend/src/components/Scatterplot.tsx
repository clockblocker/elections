import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Point, PointEstimate } from "../types";

interface View { x0: number; x1: number; y0: number; y1: number }
interface Size { width: number; height: number }
interface Drag { x: number; y: number; view: View; moved: boolean }
type PlotPoint = Point & { turnout: number; result: number };
interface ColorStop { position: number; color: [number, number, number] }

const FULL: View = { x0: 0, x1: 100, y0: 0, y1: 100 };
const MARGIN = { left: 54, right: 18, top: 20, bottom: 46 };
const COLOR_STOPS: ColorStop[] = [
  { position: 0, color: [35, 112, 91] },
  { position: 0.075, color: [75, 118, 169] },
  { position: 0.325, color: [199, 150, 51] },
  { position: 0.5, color: [204, 101, 42] },
  { position: 0.75, color: [183, 69, 46] },
  { position: 1, color: [112, 31, 29] }
];

function bounded(view: View): View {
  const width = Math.min(100, Math.max(2, view.x1 - view.x0));
  const height = Math.min(100, Math.max(2, view.y1 - view.y0));
  let x0 = view.x0; let y0 = view.y0;
  if (x0 < 0) x0 = 0; if (x0 + width > 100) x0 = 100 - width;
  if (y0 < 0) y0 = 0; if (y0 + height > 100) y0 = 100 - height;
  return { x0, x1: x0 + width, y0, y1: y0 + height };
}

function isPlottable(point: Point): point is PlotPoint {
  return point.turnout !== null && point.result !== null
    && Number.isFinite(point.turnout) && Number.isFinite(point.result);
}

function pSusOf(estimate: PointEstimate | undefined): number {
  return estimate?.status === "scored" && estimate.pSus !== null ? estimate.pSus : -1;
}

function tailPosition(pSus: number): number {
  if (pSus < 0) return -1;
  // A log-tail axis gives useful visual room to 95%, 99%, and the P3 tail.
  return Math.min(1, -Math.log10(Math.max(1e-4, 1 - Math.min(1, pSus))) / 4);
}

function gradientColor(pSus: number): string {
  const position = tailPosition(pSus);
  if (position < 0) return "rgba(95,99,93,.22)";
  const upperIndex = COLOR_STOPS.findIndex((stop) => stop.position >= position);
  const upper = COLOR_STOPS[Math.max(0, upperIndex)];
  const lower = COLOR_STOPS[Math.max(0, upperIndex - 1)];
  const fraction = upper.position === lower.position
    ? 0 : (position - lower.position) / (upper.position - lower.position);
  const channels = lower.color.map((channel, index) =>
    Math.round(channel + fraction * (upper.color[index] - channel)));
  const alpha = 0.22 + 0.5 * position;
  return `rgba(${channels[0]},${channels[1]},${channels[2]},${alpha.toFixed(3)})`;
}

export function Scatterplot({
  points, estimates, coreContour50, coreContour95, selectedId, onSelect
}: {
  points: Point[];
  estimates: Map<string, PointEstimate>;
  coreContour50: Array<{ turnout: number; result: number }>;
  coreContour95: Array<{ turnout: number; result: number }>;
  selectedId: string | null;
  onSelect(point: Point): void;
}) {
  const shellRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<Drag | null>(null);
  const viewRef = useRef<View>(FULL);
  const [view, setView] = useState<View>(FULL);
  const [size, setSize] = useState<Size>({ width: 900, height: 560 });
  const [hover, setHover] = useState<{ point: PlotPoint; x: number; y: number } | null>(null);
  const plottedPoints = useMemo(() => points.filter(isPlottable), [points]);
  const paintOrder = useMemo(() => [...plottedPoints].sort((left, right) =>
    pSusOf(estimates.get(left.id)) - pSusOf(estimates.get(right.id))), [estimates, plottedPoints]);

  const applyView = useCallback((next: View) => {
    const value = bounded(next); viewRef.current = value; setView(value);
  }, []);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const resize = () => setSize({ width: Math.max(360, shell.clientWidth), height: Math.max(420, shell.clientHeight) });
    resize();
    const observer = new ResizeObserver(resize); observer.observe(shell);
    return () => observer.disconnect();
  }, []);

  useEffect(() => { applyView(FULL); }, [points, applyView]);

  const dimensions = useCallback(() => ({
    width: Math.max(1, size.width - MARGIN.left - MARGIN.right),
    height: Math.max(1, size.height - MARGIN.top - MARGIN.bottom)
  }), [size]);
  const toPixel = useCallback((point: Pick<PlotPoint, "turnout" | "result">, current = viewRef.current) => {
    const d = dimensions();
    return {
      x: MARGIN.left + (point.turnout - current.x0) / (current.x1 - current.x0) * d.width,
      y: MARGIN.top + (current.y1 - point.result) / (current.y1 - current.y0) * d.height
    };
  }, [dimensions]);
  const fromPixel = useCallback((x: number, y: number, current = viewRef.current) => {
    const d = dimensions();
    return {
      turnout: current.x0 + (x - MARGIN.left) / d.width * (current.x1 - current.x0),
      result: current.y1 - (y - MARGIN.top) / d.height * (current.y1 - current.y0)
    };
  }, [dimensions]);

  const index = useMemo(() => {
    const cells = new Map<string, PlotPoint[]>();
    for (const point of plottedPoints) {
      const key = `${Math.floor(point.turnout)}:${Math.floor(point.result)}`;
      cells.set(key, [...(cells.get(key) ?? []), point]);
    }
    return cells;
  }, [plottedPoints]);

  const hitTest = useCallback((x: number, y: number): PlotPoint | null => {
    const data = fromPixel(x, y); const current = viewRef.current; const d = dimensions();
    const toleranceX = 9 / d.width * (current.x1 - current.x0);
    const toleranceY = 9 / d.height * (current.y1 - current.y0);
    let nearest: PlotPoint | null = null; let distance = 100;
    for (let ix = Math.floor(data.turnout - toleranceX); ix <= Math.floor(data.turnout + toleranceX); ix += 1) {
      for (let iy = Math.floor(data.result - toleranceY); iy <= Math.floor(data.result + toleranceY); iy += 1) {
        for (const point of index.get(`${ix}:${iy}`) ?? []) {
          const pixel = toPixel(point); const next = (pixel.x - x) ** 2 + (pixel.y - y) ** 2;
          if (next < distance) { nearest = point; distance = next; }
        }
      }
    }
    return nearest;
  }, [dimensions, fromPixel, index, toPixel]);

  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(size.width * dpr); canvas.height = Math.round(size.height * dpr);
    const context = canvas.getContext("2d"); if (!context) return;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, size.width, size.height);
    context.fillStyle = "#fbfaf6"; context.fillRect(0, 0, size.width, size.height);
    const current = view; const d = dimensions();
    const x = (value: number) => MARGIN.left + (value - current.x0) / (current.x1 - current.x0) * d.width;
    const y = (value: number) => MARGIN.top + (current.y1 - value) / (current.y1 - current.y0) * d.height;

    context.save(); context.beginPath(); context.rect(MARGIN.left, MARGIN.top, d.width, d.height); context.clip();
    context.strokeStyle = "rgba(31, 38, 35, .12)"; context.lineWidth = 1;
    for (let tick = 0; tick <= 100; tick += 10) {
      if (tick >= current.x0 && tick <= current.x1) { context.beginPath(); context.moveTo(x(tick), MARGIN.top); context.lineTo(x(tick), MARGIN.top + d.height); context.stroke(); }
      if (tick >= current.y0 && tick <= current.y1) { context.beginPath(); context.moveTo(MARGIN.left, y(tick)); context.lineTo(MARGIN.left + d.width, y(tick)); context.stroke(); }
    }
    // Sort once by continuous severity so extreme points are painted on top.
    for (const point of paintOrder) {
      if (point.turnout < current.x0 || point.turnout > current.x1 || point.result < current.y0 || point.result > current.y1) continue;
      const pSus = pSusOf(estimates.get(point.id)); const position = tailPosition(pSus);
      const pixel = toPixel(point, current); const width = position < 0 ? 2.25 : 2.25 + 0.75 * position;
      context.fillStyle = gradientColor(pSus);
      context.fillRect(pixel.x - width / 2, pixel.y - width / 2, width, width);
    }
    for (const { points: corePoints, color: contourColor, width } of [
      { points: coreContour95, color: "rgba(35,92,82,.74)", width: 1.75 },
      { points: coreContour50, color: "rgba(38,174,82,.96)", width: 2.75 }
    ]) {
      context.strokeStyle = contourColor; context.lineWidth = width;
      context.beginPath();
      for (const [index, corePoint] of corePoints.entries()) {
        if (index === 0) context.moveTo(x(corePoint.turnout), y(corePoint.result));
        else context.lineTo(x(corePoint.turnout), y(corePoint.result));
      }
      context.stroke();
    }
    const selected = selectedId ? plottedPoints.find((point) => point.id === selectedId) : null;
    if (selected) {
      const pixel = toPixel(selected, current); const estimate = estimates.get(selected.id);
      if (estimate?.expectedShare !== null && estimate?.expectedShare !== undefined
        && estimate.expectedTurnout !== null && estimate.expectedTurnout !== undefined && estimate.interval95) {
        const expectedResult = 100 * estimate.expectedShare;
        const lowResult = 100 * estimate.interval95[0] / selected.validBallots;
        const highResult = 100 * estimate.interval95[1] / selected.validBallots;
        const expectedPixel = toPixel({ turnout: estimate.expectedTurnout, result: expectedResult }, current);
        context.strokeStyle = "rgba(21,26,24,.78)"; context.lineWidth = 2;
        context.setLineDash([5, 4]);
        context.beginPath(); context.moveTo(expectedPixel.x, expectedPixel.y); context.lineTo(pixel.x, pixel.y); context.stroke();
        context.setLineDash([]);
        context.beginPath(); context.moveTo(expectedPixel.x, y(lowResult)); context.lineTo(expectedPixel.x, y(highResult)); context.stroke();
        context.fillStyle = "#fbfaf6"; context.strokeStyle = "#151a18";
        context.beginPath(); context.arc(expectedPixel.x, expectedPixel.y, 4, 0, Math.PI * 2); context.fill(); context.stroke();
      }
      context.strokeStyle = "#151a18"; context.lineWidth = 2;
      context.beginPath(); context.arc(pixel.x, pixel.y, 6.5, 0, Math.PI * 2); context.stroke();
    }
    context.restore();

    context.fillStyle = "#5f635d"; context.font = "11px ui-monospace, SFMono-Regular, monospace";
    context.textAlign = "center";
    for (let index = 0; index <= 5; index += 1) {
      const value = current.x0 + index / 5 * (current.x1 - current.x0);
      context.fillText(value.toFixed(current.x1 - current.x0 < 20 ? 1 : 0), MARGIN.left + index / 5 * d.width, size.height - 19);
    }
    context.textAlign = "right";
    for (let index = 0; index <= 5; index += 1) {
      const value = current.y0 + index / 5 * (current.y1 - current.y0);
      context.fillText(value.toFixed(current.y1 - current.y0 < 20 ? 1 : 0), MARGIN.left - 9, MARGIN.top + d.height - index / 5 * d.height + 4);
    }
  }, [coreContour50, coreContour95, dimensions, estimates, paintOrder, plottedPoints, selectedId, size, toPixel, view]);

  useEffect(() => {
    const shell = shellRef.current; if (!shell) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = shell.getBoundingClientRect(); const anchor = fromPixel(event.clientX - rect.left, event.clientY - rect.top);
      const current = viewRef.current; const factor = Math.exp(event.deltaY * 0.0015);
      applyView({
        x0: anchor.turnout - (anchor.turnout - current.x0) * factor,
        x1: anchor.turnout + (current.x1 - anchor.turnout) * factor,
        y0: anchor.result - (anchor.result - current.y0) * factor,
        y1: anchor.result + (current.y1 - anchor.result) * factor
      });
    };
    shell.addEventListener("wheel", onWheel, { passive: false });
    return () => shell.removeEventListener("wheel", onWheel);
  }, [applyView, fromPixel]);

  const pointer = (event: React.PointerEvent) => {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const onPointerDown = (event: React.PointerEvent) => {
    const value = pointer(event); event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { ...value, view: viewRef.current, moved: false };
  };
  const onPointerMove = (event: React.PointerEvent) => {
    const value = pointer(event); const drag = dragRef.current;
    if (drag) {
      const d = dimensions(); const dx = (value.x - drag.x) / d.width * (drag.view.x1 - drag.view.x0); const dy = (value.y - drag.y) / d.height * (drag.view.y1 - drag.view.y0);
      drag.moved ||= Math.abs(value.x - drag.x) + Math.abs(value.y - drag.y) > 4;
      applyView({ x0: drag.view.x0 - dx, x1: drag.view.x1 - dx, y0: drag.view.y0 + dy, y1: drag.view.y1 + dy });
    } else {
      const point = hitTest(value.x, value.y); setHover(point ? { point, ...value } : null);
    }
  };
  const onPointerUp = (event: React.PointerEvent) => {
    const value = pointer(event); const drag = dragRef.current; dragRef.current = null;
    if (!drag?.moved) { const point = hitTest(value.x, value.y); if (point) onSelect(point); }
  };

  return <section className="plot-card">
    <div className="plot-head">
      <div><span className="eyebrow">Physical UIK evidence field</span><h2>Turnout × option result</h2></div>
      <div className="plot-tools"><span className="gradient-key"><i /><small>P_sus · 0 · 50 · P1 95 · P2 99 · P3 99.9%+</small></span><button onClick={() => applyView(FULL)}>Reset view</button></div>
    </div>
    <div className="plot-shell" ref={shellRef} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { dragRef.current = null; }} onPointerLeave={() => setHover(null)}>
      <canvas ref={canvasRef} aria-label={`${plottedPoints.length.toLocaleString()} physical precinct points. Turnout on the horizontal axis and option result on the vertical axis.`} role="img" />
      <span className="axis axis-x">Turnout · ballots found / registered voters · %</span>
      <span className="axis axis-y">Option votes / valid ballots · %</span>
      {hover && <div className="point-tooltip" style={{ left: Math.min(hover.x + 14, size.width - 230), top: Math.max(10, hover.y - 108) }}>
        <strong>UIK {hover.point.uikNumber}</strong><span>{hover.point.regionName}</span><span>{hover.point.tikName}</span>
        <div><b>{hover.point.turnout.toFixed(2)}%</b> turnout <b>{hover.point.result.toFixed(2)}%</b> result</div>
        {estimates.get(hover.point.id)?.status === "scored" && <div><b>{estimates.get(hover.point.id)?.grade}</b> P_sus {((estimates.get(hover.point.id)?.pSus ?? 0) * 100).toFixed(2)}%</div>}
        <small>Click to inspect protocol</small>
      </div>}
    </div>
    <footer className="plot-foot"><span>{plottedPoints.length.toLocaleString()} plotted / {points.length.toLocaleString()} protocols</span><span>Bright / dark contours = fitted 50% / 95%</span><span>Selected line = core expectation → actual</span><span>Wheel to zoom · drag to pan</span></footer>
  </section>;
}
