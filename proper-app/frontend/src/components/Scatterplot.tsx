import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AnalysisParameters, Point, PointEstimate } from "../types";

interface View { x0: number; x1: number; y0: number; y1: number }
interface Size { width: number; height: number }
interface Drag { x: number; y: number; view: View; moved: boolean }

const FULL: View = { x0: 0, x1: 100, y0: 0, y1: 100 };
const MARGIN = { left: 54, right: 18, top: 20, bottom: 46 };

function bounded(view: View): View {
  const width = Math.min(100, Math.max(2, view.x1 - view.x0));
  const height = Math.min(100, Math.max(2, view.y1 - view.y0));
  let x0 = view.x0; let y0 = view.y0;
  if (x0 < 0) x0 = 0; if (x0 + width > 100) x0 = 100 - width;
  if (y0 < 0) y0 = 0; if (y0 + height > 100) y0 = 100 - height;
  return { x0, x1: x0 + width, y0, y1: y0 + height };
}

function rgba(hex: string, alpha: number): string {
  const value = Number.parseInt(hex.replace("#", ""), 16);
  return `rgba(${(value >> 16) & 255},${(value >> 8) & 255},${value & 255},${alpha})`;
}

export function Scatterplot({
  points, estimates, parameters, color, selectedId, onSelect
}: {
  points: Point[];
  estimates: Map<string, PointEstimate>;
  parameters: AnalysisParameters;
  color: string;
  selectedId: string | null;
  onSelect(point: Point): void;
}) {
  const shellRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<Drag | null>(null);
  const viewRef = useRef<View>(FULL);
  const [view, setView] = useState<View>(FULL);
  const [size, setSize] = useState<Size>({ width: 900, height: 560 });
  const [hover, setHover] = useState<{ point: Point; x: number; y: number } | null>(null);

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
  const toPixel = useCallback((point: Pick<Point, "turnout" | "result">, current = viewRef.current) => {
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
    const cells = new Map<string, Point[]>();
    for (const point of points) {
      const key = `${Math.floor(point.turnout)}:${Math.floor(point.result)}`;
      cells.set(key, [...(cells.get(key) ?? []), point]);
    }
    return cells;
  }, [points]);

  const hitTest = useCallback((x: number, y: number): Point | null => {
    const data = fromPixel(x, y); const current = viewRef.current; const d = dimensions();
    const toleranceX = 9 / d.width * (current.x1 - current.x0);
    const toleranceY = 9 / d.height * (current.y1 - current.y0);
    let nearest: Point | null = null; let distance = 100;
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
    context.fillStyle = "rgba(38, 80, 72, .055)";
    context.fillRect(x(parameters.referenceTurnoutMin), MARGIN.top,
      x(parameters.referenceTurnoutMax) - x(parameters.referenceTurnoutMin), d.height);
    context.strokeStyle = "rgba(31, 38, 35, .12)"; context.lineWidth = 1;
    for (let tick = 0; tick <= 100; tick += 10) {
      if (tick >= current.x0 && tick <= current.x1) { context.beginPath(); context.moveTo(x(tick), MARGIN.top); context.lineTo(x(tick), MARGIN.top + d.height); context.stroke(); }
      if (tick >= current.y0 && tick <= current.y1) { context.beginPath(); context.moveTo(MARGIN.left, y(tick)); context.lineTo(MARGIN.left + d.width, y(tick)); context.stroke(); }
    }
    context.strokeStyle = "#c55f3d"; context.setLineDash([6, 5]); context.lineWidth = 1.4;
    context.beginPath(); context.moveTo(x(parameters.analysisTurnoutMin), MARGIN.top); context.lineTo(x(parameters.analysisTurnoutMin), MARGIN.top + d.height); context.stroke();
    context.setLineDash([]);

    const normal = rgba(color, 0.27); const excess = rgba("#b7452e", 0.46);
    for (const point of points) {
      if (point.turnout < current.x0 || point.turnout > current.x1 || point.result < current.y0 || point.result > current.y1) continue;
      const pixel = toPixel(point, current); const estimate = estimates.get(point.id);
      context.fillStyle = estimate && estimate.excessVotes > 0 ? excess : normal;
      context.fillRect(pixel.x - 1.25, pixel.y - 1.25, 2.5, 2.5);
    }
    const selected = selectedId ? points.find((point) => point.id === selectedId) : null;
    if (selected) {
      const pixel = toPixel(selected, current); context.strokeStyle = "#151a18"; context.lineWidth = 2;
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
  }, [color, dimensions, estimates, parameters, points, selectedId, size, toPixel, view]);

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
      <div><span className="eyebrow">Physical UIK evidence field</span><h2>Turnout × party result</h2></div>
      <div className="plot-tools"><span><i className="legend-dot ordinary" style={{ background: color }} /> all UIKs</span><span><i className="legend-dot excess" /> positive deviation</span><button onClick={() => applyView(FULL)}>Reset view</button></div>
    </div>
    <div className="plot-shell" ref={shellRef} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { dragRef.current = null; }} onPointerLeave={() => setHover(null)}>
      <canvas ref={canvasRef} aria-label={`${points.length.toLocaleString()} physical precinct points. Turnout on the horizontal axis and party result on the vertical axis.`} role="img" />
      <span className="axis axis-x">Turnout · ballots found / registered voters · %</span>
      <span className="axis axis-y">Party votes / valid ballots · %</span>
      {hover && <div className="point-tooltip" style={{ left: Math.min(hover.x + 14, size.width - 230), top: Math.max(10, hover.y - 108) }}>
        <strong>UIK {hover.point.uikNumber}</strong><span>{hover.point.regionName}</span><span>{hover.point.tikName}</span>
        <div><b>{hover.point.turnout.toFixed(2)}%</b> turnout <b>{hover.point.result.toFixed(2)}%</b> result</div>
        <small>Click to inspect protocol</small>
      </div>}
    </div>
    <footer className="plot-foot"><span>{points.length.toLocaleString()} physical UIKs</span><span>Green band = reference turnout</span><span>Red rule = analysis threshold</span><span>Wheel to zoom · drag to pan</span></footer>
  </section>;
}
