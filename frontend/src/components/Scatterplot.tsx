import createREGL, { type Regl } from "regl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Party, Point } from "../types";

type View = { x0: number; x1: number; y0: number; y1: number };
type Drag = { startX: number; startY: number; view: View; box: boolean } | null;
const FULL_VIEW: View = { x0: 0, x1: 100, y0: 0, y1: 100 };
const MARGIN = { left: 52, right: 20, top: 18, bottom: 46 };

interface Props {
  points: Point[];
  parties: Party[];
  selectedId: string | null;
  onSelect(point: Point): void;
  pointSize: number;
  showGrid: boolean;
  registerPng(getPng: () => string): void;
}

export function Scatterplot({ points, parties, selectedId, onSelect, pointSize, showGrid, registerPng }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);
  const reglRef = useRef<Regl | null>(null);
  const drawRef = useRef<(() => void) | null>(null);
  const dragRef = useRef<Drag>(null);
  const [view, setView] = useState<View>(FULL_VIEW);
  const viewRef = useRef(view);
  const [size, setSize] = useState({ width: 900, height: 600 });
  const [hover, setHover] = useState<{ point: Point; candidates: Point[]; x: number; y: number } | null>(null);
  const [box, setBox] = useState<{ x: number; y: number; width: number; height: number } | null>(null);
  const [mode, setMode] = useState<"pan" | "box">("pan");

  useEffect(() => { viewRef.current = view; drawRef.current?.(); }, [view]);
  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const update = () => setSize({ width: Math.max(320, shell.clientWidth), height: Math.max(340, shell.clientHeight) });
    update();
    const observer = new ResizeObserver(update);
    observer.observe(shell);
    return () => observer.disconnect();
  }, []);

  const partyColors = useMemo(() => new Map(parties.map((party) => [party.id, hexToRgb(party.color)])), [parties]);
  const pointIndex = useMemo(() => {
    const grid = new Map<string, Point[]>();
    const overlaps = new Map<string, Point[]>();
    for (const point of points) {
      const cellKey = `${Math.floor(point.turnout)}:${Math.floor(point.partyShare)}`;
      const coordinateKey = `${point.turnout.toFixed(5)}:${point.partyShare.toFixed(5)}`;
      const cell = grid.get(cellKey); if (cell) cell.push(point); else grid.set(cellKey, [point]);
      const overlap = overlaps.get(coordinateKey); if (overlap) overlap.push(point); else overlaps.set(coordinateKey, [point]);
    }
    return { grid, overlaps };
  }, [points]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let regl: Regl;
    try { regl = createREGL({ canvas, attributes: { antialias: true, preserveDrawingBuffer: true } }); }
    catch { return; }
    reglRef.current = regl;
    const positions = points.map((point) => [point.turnout, point.partyShare]);
    const colors = points.map((point) => partyColors.get(point.partyId) || [0.2, 0.2, 0.2, 0.8]);
    const selected = points.find((point) => point.id === selectedId);
    const drawPoints = regl({
      vert: `precision highp float;
        attribute vec2 position; attribute vec4 color; uniform vec4 domain; uniform vec2 viewport; uniform vec4 margins; uniform float pointSize; varying vec4 vColor;
        void main() { float px = margins.x + (position.x-domain.x)/(domain.y-domain.x)*(viewport.x-margins.x-margins.y); float py = margins.w + (position.y-domain.z)/(domain.w-domain.z)*(viewport.y-margins.z-margins.w); gl_Position=vec4(px/viewport.x*2.0-1.0,py/viewport.y*2.0-1.0,0,1); gl_PointSize=pointSize; vColor=color; }`,
      frag: `precision mediump float; varying vec4 vColor; void main(){ vec2 c=gl_PointCoord-0.5; if(dot(c,c)>0.25) discard; gl_FragColor=vColor; }`,
      attributes: { position: positions, color: colors },
      uniforms: {
        domain: () => [viewRef.current.x0, viewRef.current.x1, viewRef.current.y0, viewRef.current.y1],
        viewport: () => [canvas.width, canvas.height], margins: [MARGIN.left, MARGIN.right, MARGIN.top, MARGIN.bottom], pointSize,
      },
      count: points.length, primitive: "points", blend: { enable: true, func: { srcRGB: "src alpha", srcAlpha: 1, dstRGB: "one minus src alpha", dstAlpha: 1 } },
    });
    const drawSelected = selected ? regl({
      vert: `precision highp float; attribute vec2 position; uniform vec4 domain; uniform vec2 viewport; uniform vec4 margins; void main(){ float px=margins.x+(position.x-domain.x)/(domain.y-domain.x)*(viewport.x-margins.x-margins.y); float py=margins.w+(position.y-domain.z)/(domain.w-domain.z)*(viewport.y-margins.z-margins.w); gl_Position=vec4(px/viewport.x*2.0-1.0,py/viewport.y*2.0-1.0,0,1); gl_PointSize=12.0; }`,
      frag: `precision mediump float; void main(){ vec2 c=gl_PointCoord-0.5; float d=length(c); if(d>.5||d<.32) discard; gl_FragColor=vec4(.08,.07,.06,1); }`,
      attributes: { position: [[selected.turnout, selected.partyShare]] },
      uniforms: { domain: () => [viewRef.current.x0, viewRef.current.x1, viewRef.current.y0, viewRef.current.y1], viewport: () => [canvas.width, canvas.height], margins: [MARGIN.left, MARGIN.right, MARGIN.top, MARGIN.bottom] },
      count: 1, primitive: "points",
    }) : null;
    const draw = () => { regl.poll(); regl.clear({ color: [0.975, 0.965, 0.93, 1], depth: 1 }); drawPoints(); drawSelected?.(); };
    drawRef.current = draw;
    draw();
    registerPng(() => canvas.toDataURL("image/png"));
    return () => { drawRef.current = null; regl.destroy(); reglRef.current = null; };
  }, [points, partyColors, pointSize, selectedId, size, registerPng]);

  const dimensions = useCallback(() => ({ width: size.width - MARGIN.left - MARGIN.right, height: size.height - MARGIN.top - MARGIN.bottom }), [size]);
  const fromPixel = useCallback((x: number, y: number, current = viewRef.current) => {
    const d = dimensions();
    return { x: current.x0 + ((x - MARGIN.left) / d.width) * (current.x1 - current.x0), y: current.y0 + ((size.height - y - MARGIN.bottom) / d.height) * (current.y1 - current.y0) };
  }, [dimensions, size.height]);
  const toPixel = useCallback((point: Point) => {
    const d = dimensions(); const current = viewRef.current;
    return { x: MARGIN.left + (point.turnout - current.x0) / (current.x1 - current.x0) * d.width, y: size.height - MARGIN.bottom - (point.partyShare - current.y0) / (current.y1 - current.y0) * d.height };
  }, [dimensions, size.height]);

  const hitTest = useCallback((x: number, y: number) => {
    let nearest: Point | null = null; let nearestDistance = Math.max(8, pointSize + 5) ** 2;
    const data = fromPixel(x, y); const d = dimensions(); const current = viewRef.current;
    const toleranceX = Math.max(1, Math.max(8, pointSize + 5) / d.width * (current.x1 - current.x0));
    const toleranceY = Math.max(1, Math.max(8, pointSize + 5) / d.height * (current.y1 - current.y0));
    for (let bx = Math.floor(data.x - toleranceX); bx <= Math.floor(data.x + toleranceX); bx += 1) {
      for (let by = Math.floor(data.y - toleranceY); by <= Math.floor(data.y + toleranceY); by += 1) {
        const candidates = pointIndex.grid.get(`${bx}:${by}`) || [];
        for (const point of candidates) {
          const pixel = toPixel(point); const distance = (pixel.x - x) ** 2 + (pixel.y - y) ** 2;
          if (distance <= nearestDistance) { nearestDistance = distance; nearest = point; }
        }
      }
    }
    if (!nearest) return null;
    const candidates = pointIndex.overlaps.get(`${nearest.turnout.toFixed(5)}:${nearest.partyShare.toFixed(5)}`) || [nearest];
    return { point: nearest, candidates };
  }, [dimensions, fromPixel, pointIndex, pointSize, toPixel]);

  const pointerPosition = (event: React.PointerEvent) => { const rect = event.currentTarget.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; };
  const onPointerDown = (event: React.PointerEvent) => {
    const p = pointerPosition(event); event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { startX: p.x, startY: p.y, view: { ...viewRef.current }, box: mode === "box" || event.shiftKey };
    if (dragRef.current.box) setBox({ x: p.x, y: p.y, width: 0, height: 0 });
  };
  const onPointerMove = (event: React.PointerEvent) => {
    const p = pointerPosition(event); const drag = dragRef.current;
    if (drag) {
      if (drag.box) setBox({ x: Math.min(p.x, drag.startX), y: Math.min(p.y, drag.startY), width: Math.abs(p.x - drag.startX), height: Math.abs(p.y - drag.startY) });
      else {
        const d = dimensions(); const dx = (p.x - drag.startX) / d.width * (drag.view.x1 - drag.view.x0); const dy = (p.y - drag.startY) / d.height * (drag.view.y1 - drag.view.y0);
        setView({ x0: drag.view.x0 - dx, x1: drag.view.x1 - dx, y0: drag.view.y0 + dy, y1: drag.view.y1 + dy });
      }
      return;
    }
    const hit = hitTest(p.x, p.y); setHover(hit ? { ...hit, x: p.x, y: p.y } : null);
  };
  const onPointerUp = (event: React.PointerEvent) => {
    const p = pointerPosition(event); const drag = dragRef.current; dragRef.current = null;
    if (drag?.box && Math.abs(p.x - drag.startX) > 8 && Math.abs(p.y - drag.startY) > 8) {
      const a = fromPixel(Math.min(p.x, drag.startX), Math.max(p.y, drag.startY), drag.view); const b = fromPixel(Math.max(p.x, drag.startX), Math.min(p.y, drag.startY), drag.view);
      setView({ x0: a.x, x1: b.x, y0: a.y, y1: b.y });
    } else if (!drag || (Math.abs(p.x - drag.startX) < 4 && Math.abs(p.y - drag.startY) < 4)) { const hit = hitTest(p.x, p.y); if (hit) onSelect(hit.point); }
    setBox(null);
  };
  const onWheel = (event: React.WheelEvent) => {
    event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect(); const px = event.clientX - rect.left; const py = event.clientY - rect.top;
    const anchor = fromPixel(px, py); const current = viewRef.current; const factor = Math.exp(event.deltaY * 0.0015);
    setView({ x0: anchor.x - (anchor.x - current.x0) * factor, x1: anchor.x + (current.x1 - anchor.x) * factor, y0: anchor.y - (anchor.y - current.y0) * factor, y1: anchor.y + (current.y1 - anchor.y) * factor });
  };
  const fitSelected = () => {
    const selected = points.find((point) => point.id === selectedId || point.uikId === selectedId);
    if (selected) setView({ x0: Math.max(0, selected.turnout - 7), x1: Math.min(100, selected.turnout + 7), y0: Math.max(0, selected.partyShare - 7), y1: Math.min(100, selected.partyShare + 7) });
  };
  const ticks = [0, 20, 40, 60, 80, 100];
  const reference = useMemo(() => {
    const voters = points.reduce((sum, point) => sum + point.registeredVoters, 0);
    const ballots = points.reduce((sum, point) => sum + point.ballotsIssued, 0);
    const partyVotes = points.reduce((sum, point) => sum + point.partyVotes, 0);
    return { turnout: voters ? ballots / voters * 100 : 0, result: ballots ? partyVotes / ballots * 100 : 0 };
  }, [points]);
  const candidateMode = points.some((point) => point.ballotKind === "single_member");
  const resultLabel = candidateMode ? "candidate result" : "party result";
  return <section className="plot-card" aria-label={`Turnout and ${resultLabel} scatterplot`}>
    <div className="plot-toolbar">
      <div><span className="eyebrow">UIK evidence graph</span><h2>Turnout × {resultLabel}</h2></div>
      <div className="tool-buttons" role="toolbar" aria-label="Chart navigation">
        <button className={mode === "pan" ? "active" : ""} aria-pressed={mode === "pan"} onClick={() => setMode("pan")}>↔ Pan</button>
        <button className={mode === "box" ? "active" : ""} aria-pressed={mode === "box"} onClick={() => setMode("box")}>⌗ Box</button>
        <button onClick={() => setView(FULL_VIEW)}>Reset</button>
        <button onClick={fitSelected} disabled={!selectedId}>Fit selected</button>
      </div>
    </div>
    <div className={`plot-shell mode-${mode}`} ref={shellRef} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => { dragRef.current = null; setBox(null); }} onPointerLeave={() => setHover(null)} onWheel={onWheel}>
      <svg className="plot-grid" width={size.width} height={size.height} aria-hidden="true">{showGrid && ticks.map((tick) => <g key={`x-${tick}`}><line x1={MARGIN.left + tick / 100 * (size.width - MARGIN.left - MARGIN.right)} y1={MARGIN.top} x2={MARGIN.left + tick / 100 * (size.width - MARGIN.left - MARGIN.right)} y2={size.height - MARGIN.bottom} /><text x={MARGIN.left + tick / 100 * (size.width - MARGIN.left - MARGIN.right)} y={size.height - 19}>{Math.round(view.x0 + tick / 100 * (view.x1 - view.x0))}</text></g>)}{showGrid && ticks.map((tick) => <g key={`y-${tick}`}><line x1={MARGIN.left} y1={MARGIN.top + tick / 100 * (size.height - MARGIN.top - MARGIN.bottom)} x2={size.width - MARGIN.right} y2={MARGIN.top + tick / 100 * (size.height - MARGIN.top - MARGIN.bottom)} /><text x={MARGIN.left - 10} y={size.height - MARGIN.bottom - tick / 100 * (size.height - MARGIN.top - MARGIN.bottom)}>{Math.round(view.y0 + tick / 100 * (view.y1 - view.y0))}</text></g>)}
        {reference.turnout >= view.x0 && reference.turnout <= view.x1 && <line className="reference-line" x1={MARGIN.left + (reference.turnout - view.x0) / (view.x1 - view.x0) * (size.width - MARGIN.left - MARGIN.right)} x2={MARGIN.left + (reference.turnout - view.x0) / (view.x1 - view.x0) * (size.width - MARGIN.left - MARGIN.right)} y1={MARGIN.top} y2={size.height - MARGIN.bottom} />}
        {reference.result >= view.y0 && reference.result <= view.y1 && <line className="reference-line" x1={MARGIN.left} x2={size.width - MARGIN.right} y1={size.height - MARGIN.bottom - (reference.result - view.y0) / (view.y1 - view.y0) * (size.height - MARGIN.top - MARGIN.bottom)} y2={size.height - MARGIN.bottom - (reference.result - view.y0) / (view.y1 - view.y0) * (size.height - MARGIN.top - MARGIN.bottom)} />}
      </svg>
      <canvas ref={canvasRef} width={size.width} height={size.height} aria-label={`${points.length.toLocaleString()} precinct observations. Turnout on x-axis; ${resultLabel} on y-axis.`} role="img" />
      <span className="axis-label axis-x">Turnout, % of registered voters</span><span className="axis-label axis-y">{candidateMode ? "Candidate" : "Party"} result, % of valid votes</span>
      {box && <span className="zoom-box" style={box} />}
      {hover && <HoverCard {...hover} onSelect={onSelect} />}
    </div>
    <footer className="plot-caption"><span><strong>{points.length.toLocaleString()}</strong> UIK–{candidateMode ? "candidate" : "party"} observations</span><span>One mark = one UIK protocol result</span><span>Wheel/pinch to zoom · drag to pan · Shift+drag to box zoom</span></footer>
  </section>;
}

function HoverCard({ point, candidates, x, y, onSelect }: { point: Point; candidates: Point[]; x: number; y: number; onSelect(point: Point): void }) {
  return <div className="hover-card" style={{ left: x, top: y }} role="tooltip">
    <div className="hover-title"><strong>UIK {point.uikNumber}</strong><span className={`match-dot ${point.matchStatus}`} />{point.matchStatus}</div><p>{point.tikName}<br />{point.regionName}</p>
    <div className="hover-metrics"><span>Turnout <strong>{point.turnout.toFixed(1)}%</strong></span><span>{point.partyName} <strong>{point.partyVotes.toLocaleString()} · {point.partyShare.toFixed(1)}%</strong></span><span>Registered <strong>{point.registeredVoters.toLocaleString()}</strong></span></div>
    <p>{point.specialFlags.length ? point.specialFlags.join(" · ") : "Standard physical precinct"}</p>
    {candidates.length > 1 && <div className="overlap-list"><strong>{candidates.length} overlapping records</strong>{candidates.map((candidate) => <button key={candidate.id} onPointerDown={(event) => event.stopPropagation()} onClick={() => onSelect(candidate)}>UIK {candidate.uikNumber} · {candidate.partyName}</button>)}</div>}
  </div>;
}

function hexToRgb(hex: string): [number, number, number, number] {
  const normalized = hex.replace("#", ""); const value = Number.parseInt(normalized.length === 3 ? normalized.split("").map((x) => x + x).join("") : normalized, 16);
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255, 0.68];
}
