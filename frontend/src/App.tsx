import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { demoDetail, demoMetadata, demoPoints, demoStatus } from "./demo";
import { DetailPanel } from "./components/DetailPanel";
import { Filters } from "./components/Filters";
import { Scatterplot } from "./components/Scatterplot";
import { exportCsv, exportJson, exportPng } from "./exports";
import { DEFAULT_STATE, parseAnalyticalState, readPreferences, savePreferences, serializeAnalyticalState } from "./state";
import type { AnalyticalState, DatasetStatus, FilterMetadata, Point, PrecinctDetail } from "./types";
import "./styles.css";

export default function App() {
  const [state, setState] = useState(() => parseAnalyticalState(window.location.search));
  const [preferences, setPreferences] = useState(readPreferences);
  const [metadata, setMetadata] = useState<FilterMetadata | null>(null);
  const [status, setStatus] = useState<DatasetStatus | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [detail, setDetail] = useState<PrecinctDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [demo, setDemo] = useState(false);
  const [retry, setRetry] = useState(0);
  const [search, setSearch] = useState("");
  const pngRef = useRef<() => string>(() => "");
  const registerPng = useCallback((getPng: () => string) => { pngRef.current = getPng; }, []);

  useEffect(() => { window.history.replaceState(null, "", `${window.location.pathname}${serializeAnalyticalState(state)}`); }, [state]);
  useEffect(() => { savePreferences(preferences); }, [preferences]);
  useEffect(() => {
    const onPopState = () => setState(parseAnalyticalState(window.location.search));
    window.addEventListener("popstate", onPopState); return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    if (demo) { setMetadata(demoMetadata); setStatus(demoStatus); setPoints(demoPoints(state)); setLoading(false); setError(null); return; }
    const controller = new AbortController(); setLoading(true); setError(null);
    Promise.all([api.status(controller.signal), api.filters(controller.signal)])
      .then(async ([nextStatus, nextMetadata]) => {
        const defaultParty = nextMetadata.parties.find((party) => /united russia/i.test(party.name));
        const resolvedState = state.partyIds.includes("united-russia") && defaultParty ? { ...state, partyIds: [defaultParty.id] } : state;
        if (resolvedState !== state) setState(resolvedState);
        const nextPoints = await api.points(resolvedState, nextMetadata, controller.signal);
        return [nextStatus, { ...nextMetadata, sourceVersion: nextStatus.sourceVersion }, nextPoints] as const;
      })
      .then(([nextStatus, nextMetadata, nextPoints]) => { setStatus(nextStatus); setMetadata(nextMetadata); setPoints(nextPoints); })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "The API did not return a response."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [demo, retry, state]);

  const selectedPoint = useMemo(() => points.find((point) => point.id === state.selectedId || point.uikId === state.selectedId) || null, [points, state.selectedId]);
  useEffect(() => {
    if (!selectedPoint) { setDetail(null); setDetailError(null); return; }
    const controller = new AbortController(); setDetailLoading(true); setDetail(null); setDetailError(null);
    const request = demo ? Promise.resolve(demoDetail(selectedPoint.uikId)) : api.precinct(selectedPoint.uikId, controller.signal);
    request.then(setDetail).catch((reason: unknown) => { if (!controller.signal.aborted) setDetailError(reason instanceof Error ? reason.message : "Unknown error"); }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [demo, selectedPoint]);

  const selectPoint = (point: Point) => setState((current) => ({ ...current, selectedId: point.id }));
  const searchPoint = () => {
    const query = search.trim().replace(/^UIK\s*/i, "");
    const found = points.find((point) => point.uikNumber === query || point.uikId.toLowerCase() === query.toLowerCase());
    if (found) selectPoint(found);
  };
  const appliedCount = state.regionIds.length + state.tikIds.length + state.specialTypes.length + state.matchStatuses.length + (state.turnoutMin > 0 || state.turnoutMax < 100 ? 1 : 0) + (state.resultMin > 0 || state.resultMax < 100 ? 1 : 0);

  if (loading && !metadata) return <StatePage type="loading" title="Preparing the evidence table" body="Checking reconciliation status and streaming precinct observations…" />;
  if (error && !metadata) return <StatePage type="error" title="The local API is unavailable" body={error} actions={<><button onClick={() => setRetry((x) => x + 1)}>Try again</button><button className="secondary" onClick={() => setDemo(true)}>Open demonstration snapshot</button></>} />;
  const activeMetadata = metadata || demoMetadata;
  const activeStatus = status || demoStatus;
  return <div className={`app ${preferences.highContrast ? "high-contrast" : ""}`}>
    <header className="masthead">
      <a className="brand" href="/" aria-label="Election Fieldwork home"><span className="brand-mark">EF</span><span>Election<br />Fieldwork</span></a>
      <div className="edition"><span>Research workbench</span><strong>State Duma · 2021</strong></div>
      <div className="header-actions">
        <form className="uik-search" onSubmit={(event) => { event.preventDefault(); searchPoint(); }}><label htmlFor="uik-search">Find UIK</label><input id="uik-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Number or ID" list="uik-options" /><datalist id="uik-options">{points.slice(0, 2500).map((point) => <option key={point.id} value={point.uikNumber} />)}</datalist><button aria-label="Search precinct">→</button></form>
        <details className="menu"><summary>Display</summary><div className="popover"><label>Point size <input type="range" min="1" max="8" value={preferences.pointSize} onChange={(event) => setPreferences({ ...preferences, pointSize: Number(event.target.value) })} /></label><label><input type="checkbox" checked={preferences.showGrid} onChange={(event) => setPreferences({ ...preferences, showGrid: event.target.checked })} /> Show grid</label><label><input type="checkbox" checked={preferences.highContrast} onChange={(event) => setPreferences({ ...preferences, highContrast: event.target.checked })} /> High contrast</label></div></details>
        <details className="menu export-menu"><summary>Export ↓</summary><div className="popover"><button onClick={() => exportPng(pngRef.current())}>Graph as PNG</button><button onClick={() => exportCsv(points, activeStatus.sourceVersion, state)}>Visible points as CSV</button><button onClick={() => exportJson(state, activeStatus.sourceVersion)}>Analysis state as JSON</button></div></details>
      </div>
    </header>
    <div className="dataset-ribbon" role="status"><span className={`ready-dot ${activeStatus.ready && activeStatus.reconciled ? "ok" : "warn"}`} /><strong>{activeStatus.reconciled ? "National totals reconciled" : "Validation incomplete"}</strong><span>Source {activeStatus.sourceVersion}</span>{demo && <span className="demo-label">Demonstration data</span>}<span className="ribbon-spacer" /><span>{activeStatus.pointCount.toLocaleString()} source observations</span></div>
    {error && <div className="inline-error">API refresh failed: {error}. Showing the last successful result. <button onClick={() => setRetry((x) => x + 1)}>Retry</button></div>}
    <main className="workspace">
      <Filters state={state} metadata={activeMetadata} onChange={setState} onReset={() => setState({ ...DEFAULT_STATE })} />
      <div className="analysis">
        <div className="analysis-meta"><div><span className="eyebrow">Current view</span><h1>Precinct result field</h1></div><div className="filter-summary"><strong>{points.length.toLocaleString()}</strong><span>visible marks</span>{appliedCount > 0 && <em>{appliedCount} filters applied</em>}</div></div>
        {!loading && !error && points.length === 0 ? <div className="empty-state"><span className="index-mark">00</span><h2>No precincts match this field</h2><p>Broaden the turnout or result range, or clear geography and metadata filters.</p><button onClick={() => setState({ ...DEFAULT_STATE })}>Reset analytical state</button></div> : <Scatterplot points={points} parties={activeMetadata.parties} selectedId={state.selectedId} onSelect={selectPoint} pointSize={preferences.pointSize} showGrid={preferences.showGrid} registerPng={registerPng} />}
      </div>
      <DetailPanel point={selectedPoint} detail={detail} loading={detailLoading} error={detailError} onClose={() => setState((current) => ({ ...current, selectedId: null }))} />
    </main>
  </div>;
}

function StatePage({ type, title, body, actions }: { type: "loading" | "error"; title: string; body: string; actions?: React.ReactNode }) {
  return <main className={`state-page ${type}`}><div className="state-brand">EF / 2021</div>{type === "loading" ? <span className="loader-orbit"><i /></span> : <span className="error-glyph">!</span>}<span className="eyebrow">Research workbench</span><h1>{title}</h1><p>{body}</p>{actions && <div className="state-actions">{actions}</div>}</main>;
}
