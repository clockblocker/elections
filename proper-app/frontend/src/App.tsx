import { useEffect, useMemo, useState } from "react";
import { DEFAULT_PARAMETERS, analyze, summarizeAnalysis } from "./analysis";
import { api } from "./api";
import { ProtocolPanel } from "./components/ProtocolPanel";
import { Scatterplot } from "./components/Scatterplot";
import type { AnalysisParameters, Metadata, Point, ProtocolDetail } from "./types";
import "./styles.css";

const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });

function finiteOr(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return value !== null && Number.isFinite(parsed) ? parsed : fallback;
}

function initialQuery() {
  const query = new URLSearchParams(window.location.search);
  return {
    option: Number(query.get("party")) || null,
    region: query.get("region") || null,
    parameters: {
      ...DEFAULT_PARAMETERS,
      turnoutWindow: finiteOr(query.get("window"), DEFAULT_PARAMETERS.turnoutWindow),
      minTikPeers: finiteOr(query.get("minPeers"), DEFAULT_PARAMETERS.minTikPeers),
      fdrThreshold: finiteOr(query.get("fdr"), DEFAULT_PARAMETERS.fdrThreshold)
    } satisfies AnalysisParameters,
    selected: query.get("uik")
  };
}

export default function App() {
  const initial = useMemo(initialQuery, []);
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [optionId, setOptionId] = useState<number | null>(initial.option);
  const [region, setRegion] = useState<string | null>(initial.region);
  const [parameters, setParameters] = useState(initial.parameters);
  const [points, setPoints] = useState<Point[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initial.selected);
  const [detail, setDetail] = useState<ProtocolDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController(); setLoading(true);
    api.metadata(controller.signal).then((value) => {
      setMetadata(value);
      setOptionId((current) => value.options.some((option) => option.id === current)
        ? current : value.options.find((option) => option.position === 5)?.id ?? value.options[0]?.id ?? null);
      setRegion((current) => value.regions.some((item) => item.code === current) ? current : null);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not load metadata"))
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  // Always fit the same national family. Geography is a display slice and must not
  // change an individual protocol's baseline, calibration percentile, or q-value.
  useEffect(() => {
    if (!optionId) return;
    const controller = new AbortController(); setLoading(true); setError(null); setPoints([]); setDetail(null);
    api.points(optionId, null, controller.signal).then((next) => {
      setPoints(next);
      setSelectedId((current) => current && next.some((point) => point.id === current) ? current : null);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load points");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [optionId]);

  const visiblePoints = useMemo(
    () => region ? points.filter((point) => point.regionCode === region) : points,
    [points, region]
  );
  const selected = useMemo(
    () => visiblePoints.find((point) => point.id === selectedId) ?? null,
    [visiblePoints, selectedId]
  );
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    const controller = new AbortController(); setDetailLoading(true); setDetail(null);
    api.protocol(selected.id, controller.signal).then(setDetail)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setNotice(reason instanceof Error ? reason.message : "Could not load protocol");
      }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected]);

  const analysis = useMemo(() => {
    if (!points.length) return { value: null, error: null };
    try { return { value: analyze(points, parameters), error: null }; }
    catch (reason) { return { value: null, error: reason instanceof Error ? reason.message : "Analysis failed" }; }
  }, [parameters, points]);
  const summary = useMemo(
    () => analysis.value ? summarizeAnalysis(analysis.value, visiblePoints) : null,
    [analysis.value, visiblePoints]
  );
  const reviewQueue = useMemo(() => {
    if (!analysis.value) return [];
    return visiblePoints.map((point) => ({ point, estimate: analysis.value!.estimates.get(point.id) }))
      .filter((item) => item.estimate?.status === "scored")
      .sort((left, right) => (right.estimate?.pSus ?? -1) - (left.estimate?.pSus ?? -1))
      .slice(0, 6);
  }, [analysis.value, visiblePoints]);
  const party = metadata?.options.find((option) => option.id === optionId) ?? null;

  useEffect(() => {
    if (!optionId) return;
    const query = new URLSearchParams({
      party: String(optionId), window: String(parameters.turnoutWindow),
      minPeers: String(parameters.minTikPeers), fdr: String(parameters.fdrThreshold)
    });
    if (region) query.set("region", region); if (selectedId) query.set("uik", selectedId);
    window.history.replaceState(null, "", `${window.location.pathname}?${query}`);
  }, [optionId, parameters, region, selectedId]);

  const updateParameter = (key: keyof AnalysisParameters, value: number) => {
    setParameters((current) => ({ ...current, [key]: value }));
  };
  const copyLink = async () => {
    await navigator.clipboard.writeText(window.location.href); setNotice("Shareable analysis URL copied");
    window.setTimeout(() => setNotice(null), 2200);
  };
  const exportAnalysis = () => {
    if (!analysis.value || !party || !summary) return;
    const visibleIds = new Set(visiblePoints.map((point) => point.id));
    const estimates = Object.fromEntries(
      [...analysis.value.estimates].filter(([id]) => visibleIds.has(id))
    );
    const result = {
      election: "2021-duma", scope: "physical-uik-party-list", degIncluded: false,
      geography: region ?? "all", party: { id: party.id, position: party.position, name: party.name },
      method: analysis.value.method, parameters, calibrationPopulation: "all physical UIKs",
      pSusMeaning: "empirical percentile of the leave-one-out positive CLT residual; not probability of fraud",
      multipleTesting: "Benjamini-Yekutieli q-value", summary, estimates
    };
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    anchor.download = `2021-duma-${party.shortName.replace(/\s+/g, "-").toLowerCase()}-peer-clt-v2.json`;
    anchor.click(); URL.revokeObjectURL(anchor.href);
  };

  if (!metadata && loading) return <main className="state-page"><div className="state-mark">21</div><span className="eyebrow">Loading physical evidence</span><h1>Preparing the precinct field</h1><p>Connecting to PostgreSQL and resolving the 2021 protocol index…</p></main>;
  if (!metadata || (error && !points.length)) return <main className="state-page error"><div className="state-mark">!</div><span className="eyebrow">Workbench unavailable</span><h1>The evidence API did not answer</h1><p>{error ?? "No metadata was returned."}</p><button onClick={() => window.location.reload()}>Retry</button></main>;

  return <div className="app-shell">
    <header className="masthead">
      <a className="brand" href="/"><span className="brand-year">2021</span><span><strong>Physical Vote Field</strong><small>State Duma research edition</small></span></a>
      <div className="scope-ribbon"><i /> Physical UIKs only <span>DEG outside model</span></div>
      <div className="header-actions"><button onClick={copyLink}>Copy analysis link</button><button className="primary" onClick={exportAnalysis} disabled={!analysis.value}>Export result</button></div>
    </header>
    {notice && <div className="toast" role="status">{notice}</div>}
    <main className="workbench">
      <aside className="control-panel">
        <div className="panel-title"><span className="panel-index">01</span><div><span className="eyebrow">Model controls</span><h1>Peer-CLT parameters</h1></div></div>
        <label className="field"><span>Election</span><select disabled><option>State Duma · 2021</option></select></label>
        <label className="field"><span>Ballot</span><select disabled><option>Federal party list</option></select></label>
        <label className="field"><span>Target party</span><select value={optionId ?? ""} onChange={(event) => setOptionId(Number(event.target.value))}>{metadata.options.map((option) => <option value={option.id} key={option.id}>{option.position}. {option.shortName}</option>)}</select></label>
        <label className="field"><span>Display geography</span><select value={region ?? ""} onChange={(event) => { setRegion(event.target.value || null); setSelectedId(null); }}><option value="">All 85 regions</option>{metadata.regions.map((item) => <option value={item.code} key={item.code}>{item.code} · {item.name} · {integer.format(item.precincts)}</option>)}</select></label>

        <fieldset className="parameter-group"><legend>Comparable turnout</legend><p>Learn each result from other UIKs near its turnout, with the target UIK removed.</p><div className="threshold"><input aria-label="Peer turnout window" type="range" min="5" max="25" step="2.5" value={parameters.turnoutWindow} onChange={(event) => updateParameter("turnoutWindow", Number(event.target.value))} /><output>±{parameters.turnoutWindow}%</output></div></fieldset>
        <fieldset className="parameter-group"><legend>TIK support floor</legend><p>Sparse TIK estimates fall back to a shrunk regional peer curve.</p><div className="number-pair single-number"><label><span>Minimum peer UIKs</span><input type="number" min="2" max="40" step="1" value={parameters.minTikPeers} onChange={(event) => updateParameter("minTikPeers", Number(event.target.value))} /></label></div></fieldset>
        <label className="field parameter-select"><span>Review false-discovery rate</span><select value={parameters.fdrThreshold} onChange={(event) => updateParameter("fdrThreshold", Number(event.target.value))}><option value="0.01">1% · strict</option><option value="0.05">5% · default</option><option value="0.1">10% · exploratory</option></select></label>

        <section className="scope-note neutral-note"><span className="eyebrow">What P_sus means</span><h2>A peer-anomaly grade</h2><p>P_sus is the percentile of a protocol’s positive, overdispersion-adjusted CLT residual among all physical UIKs. It is not a probability of fraud. BY q-values separately control the review queue.</p></section>
        <section className="scope-note"><span className="eyebrow">Analysis boundary</span><h2>Why DEG is absent</h2><p>Electronic results are aggregate returns without the physical precinct distribution this model compares. They remain outside every fit and denominator.</p></section>
        <section className="coverage"><span>{metadata.coverage.importedProtocols.toLocaleString()} imported protocols</span><span>{metadata.coverage.missingProtocols} source gaps retained</span><span>{metadata.coverage.tiks.toLocaleString()} TIKs</span></section>
      </aside>

      <section className="analysis-column">
        <div className="analysis-heading"><div><span className="eyebrow">02 · Conditional CLT screen</span><h1>{party?.shortName ?? "Party"} expected × actual field</h1><p>{region ? `${metadata.regions.find((item) => item.code === region)?.name} · scores fixed to national calibration` : "Russian Federation · TIK → region → national partial pooling"}</p></div>{loading && <span className="loading-pill">Loading protocols…</span>}</div>
        {analysis.error && <div className="analysis-error">{analysis.error}</div>}
        {summary && <div className="metric-row">
          <article><span>BY review queue</span><strong>{integer.format(summary.flaggedProtocols)}</strong><small>q ≤ {percent.format(parameters.fdrThreshold)}</small></article>
          <article><span>Observed / expected</span><strong>{compact.format(summary.observedVotes)} / {compact.format(summary.expectedVotes)}</strong><small>target-party votes</small></article>
          <article><span>Flagged residual</span><strong>{integer.format(summary.flaggedResidualVotes)}</strong><small>actual − expected votes</small></article>
          <article><span>CLT coverage</span><strong>{integer.format(summary.scoredProtocols)} / {integer.format(summary.protocols)}</strong><small>{summary.unscoredProtocols} explicitly unscored</small></article>
        </div>}
        {visiblePoints.length && party && analysis.value ? <Scatterplot points={visiblePoints} estimates={analysis.value.estimates} parameters={parameters} color={party.color} selectedId={selectedId} onSelect={(point) => setSelectedId(point.id)} /> : !loading && <div className="empty-chart">No physical UIK points match this selection.</div>}
        {reviewQueue.length > 0 && <section className="review-queue"><div><span className="eyebrow">Highest peer incompatibility</span><h2>Protocol review queue</h2></div><div className="review-items">{reviewQueue.map(({ point, estimate }) => <button key={point.id} onClick={() => setSelectedId(point.id)} className={selectedId === point.id ? "selected" : ""}><span><b>UIK {point.uikNumber}</b><small>{point.regionName}</small></span><strong>{estimate?.grade}</strong><em>{percent.format(estimate?.pSus ?? 0)}</em></button>)}</div></section>}
        <section className="method-strip"><div><span className="eyebrow">Method note</span><h2>Peer-conditioned CLT · v2</h2></div><p>The expected party share is learned out of sample from similar-turnout UIKs, preferring the same TIK and shrinking sparse groups toward regional and national peers. Predictive variance includes binomial noise, baseline uncertainty, and robust TIK/region heterogeneity. P_sus is an empirical residual percentile; the review queue uses conservative dependence-aware BY q-values. Aggregate returns alone cannot identify fraud.</p><code>peer-clt-v2 · physical UIKs · DEG=false</code></section>
      </section>

      <ProtocolPanel point={selected} estimate={selected ? analysis.value?.estimates.get(selected.id) : undefined} detail={detail} loading={detailLoading} onClose={() => setSelectedId(null)} />
    </main>
  </div>;
}
