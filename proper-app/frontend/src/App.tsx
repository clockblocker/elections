import { useEffect, useMemo, useState } from "react";
import { analyze } from "./analysis";
import { api } from "./api";
import { ProtocolPanel } from "./components/ProtocolPanel";
import { Scatterplot } from "./components/Scatterplot";
import type { AnalysisParameters, Metadata, Point, ProtocolDetail } from "./types";
import "./styles.css";

const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

function initialQuery() {
  const query = new URLSearchParams(window.location.search);
  return {
    option: Number(query.get("party")) || null,
    region: query.get("region") || null,
    parameters: {
      referenceTurnoutMin: Number(query.get("refMin")) || 20,
      referenceTurnoutMax: Number(query.get("refMax")) || 50,
      analysisTurnoutMin: Number(query.get("threshold")) || 50,
      positiveExcessOnly: true
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

  useEffect(() => {
    if (!optionId) return;
    const controller = new AbortController(); setLoading(true); setError(null); setPoints([]); setDetail(null);
    api.points(optionId, region, controller.signal).then((next) => {
      setPoints(next);
      setSelectedId((current) => current && next.some((point) => point.id === current) ? current : null);
    })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load points"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [optionId, region]);

  const selected = useMemo(() => points.find((point) => point.id === selectedId) ?? null, [points, selectedId]);
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    const controller = new AbortController(); setDetailLoading(true); setDetail(null);
    api.protocol(selected.id, controller.signal).then(setDetail)
      .catch((reason: unknown) => { if (!controller.signal.aborted) setNotice(reason instanceof Error ? reason.message : "Could not load protocol"); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected]);

  const analysis = useMemo(() => {
    if (!points.length) return { value: null, error: null };
    try { return { value: analyze(points, parameters), error: null }; }
    catch (reason) { return { value: null, error: reason instanceof Error ? reason.message : "Analysis failed" }; }
  }, [parameters, points]);
  const party = metadata?.options.find((option) => option.id === optionId) ?? null;

  useEffect(() => {
    if (!optionId) return;
    const query = new URLSearchParams({
      party: String(optionId), refMin: String(parameters.referenceTurnoutMin),
      refMax: String(parameters.referenceTurnoutMax), threshold: String(parameters.analysisTurnoutMin)
    });
    if (region) query.set("region", region); if (selectedId) query.set("uik", selectedId);
    window.history.replaceState(null, "", `${window.location.pathname}?${query}`);
  }, [optionId, parameters, region, selectedId]);

  const updateParameter = (key: keyof AnalysisParameters, value: number | boolean) => setParameters((current) => ({ ...current, [key]: value }));
  const copyLink = async () => {
    await navigator.clipboard.writeText(window.location.href); setNotice("Shareable analysis URL copied");
    window.setTimeout(() => setNotice(null), 2200);
  };
  const exportAnalysis = () => {
    if (!analysis.value || !party) return;
    const result = { election: "2021-duma", scope: "physical-uik-party-list", degIncluded: false,
      party: { id: party.id, position: party.position, name: party.name }, parameters,
      result: { ...analysis.value, estimates: Object.fromEntries(analysis.value.estimates) } };
    const anchor = document.createElement("a"); anchor.href = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    anchor.download = `2021-duma-${party.shortName.replace(/\s+/g, "-").toLowerCase()}-analysis.json`; anchor.click(); URL.revokeObjectURL(anchor.href);
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
        <div className="panel-title"><span className="panel-index">01</span><div><span className="eyebrow">Model controls</span><h1>Field parameters</h1></div></div>
        <label className="field"><span>Election</span><select disabled><option>State Duma · 2021</option></select></label>
        <label className="field"><span>Ballot</span><select disabled><option>Federal party list</option></select></label>
        <label className="field"><span>Target party</span><select value={optionId ?? ""} onChange={(event) => setOptionId(Number(event.target.value))}>{metadata.options.map((option) => <option value={option.id} key={option.id}>{option.position}. {option.shortName}</option>)}</select></label>
        <label className="field"><span>Analysis geography</span><select value={region ?? ""} onChange={(event) => setRegion(event.target.value || null)}><option value="">All 85 regions</option>{metadata.regions.map((item) => <option value={item.code} key={item.code}>{item.code} · {item.name} · {integer.format(item.precincts)}</option>)}</select></label>

        <fieldset className="parameter-group"><legend>Reference turnout band</legend><p>Regional target-to-other odds are learned here.</p><div className="number-pair"><label><span>From</span><input type="number" min="0" max="99" step="1" value={parameters.referenceTurnoutMin} onChange={(event) => updateParameter("referenceTurnoutMin", Number(event.target.value))} /></label><b>—</b><label><span>To</span><input type="number" min="1" max="100" step="1" value={parameters.referenceTurnoutMax} onChange={(event) => updateParameter("referenceTurnoutMax", Number(event.target.value))} /></label></div></fieldset>
        <fieldset className="parameter-group"><legend>Analysis threshold</legend><p>Estimate deviations at and above this turnout.</p><div className="threshold"><input type="range" min="20" max="90" step="1" value={parameters.analysisTurnoutMin} onChange={(event) => updateParameter("analysisTurnoutMin", Number(event.target.value))} /><output>{parameters.analysisTurnoutMin}%</output></div></fieldset>
        <label className="switch"><input type="checkbox" checked={parameters.positiveExcessOnly} onChange={(event) => updateParameter("positiveExcessOnly", event.target.checked)} /><span><b>Positive deviations only</b><small>Do not offset positive estimates with negative ones.</small></span></label>

        <section className="scope-note"><span className="eyebrow">Analysis boundary</span><h2>Why DEG is absent</h2><p>Electronic results are aggregate returns without the physical precinct distribution this model compares. They are excluded from loading, charts, and denominators.</p></section>
        <section className="coverage"><span>{metadata.coverage.importedProtocols.toLocaleString()} imported</span><span>{metadata.coverage.missingProtocols} source gaps retained</span><span>{metadata.coverage.tiks.toLocaleString()} TIKs</span></section>
      </aside>

      <section className="analysis-column">
        <div className="analysis-heading"><div><span className="eyebrow">02 · Versioned estimate</span><h1>{party?.shortName ?? "Party"} physical precinct field</h1><p>{region ? metadata.regions.find((item) => item.code === region)?.name : "Russian Federation · 85 regional baselines where supported"}</p></div>{loading && <span className="loading-pill">Loading protocols…</span>}</div>
        {analysis.error && <div className="analysis-error">{analysis.error}</div>}
        {analysis.value && <div className="metric-row">
          <article><span>Estimated positive deviation</span><strong>{integer.format(analysis.value.estimatedExcessVotes)}</strong><small>votes · model output</small></article>
          <article><span>Observed above threshold</span><strong>{integer.format(analysis.value.observedVotes)}</strong><small>target-party votes</small></article>
          <article><span>Reference support</span><strong>{integer.format(analysis.value.referencePoints)}</strong><small>physical UIKs</small></article>
          <article><span>Pooled reference share</span><strong>{decimal.format(analysis.value.baselineShare * 100)}%</strong><small>{analysis.value.regionsWithLocalBaseline} local baselines</small></article>
        </div>}
        {points.length && party && analysis.value ? <Scatterplot points={points} estimates={analysis.value.estimates} parameters={parameters} color={party.color} selectedId={selectedId} onSelect={(point) => setSelectedId(point.id)} /> : !loading && <div className="empty-chart">No physical UIK points match this selection.</div>}
        <section className="method-strip"><div><span className="eyebrow">Method note</span><h2>Regional baseline odds · v1</h2></div><p>For each region, the model learns target-party votes relative to all other valid votes inside the reference turnout band. Above the threshold, it holds observed non-target votes fixed and estimates the target vote implied by those odds. Positive differences are summed. This is a sensitivity model—not a precinct-level finding of fraud.</p><code>shpilkin-odds-v1 · physical UIKs · DEG=false</code></section>
      </section>

      <ProtocolPanel point={selected} estimate={selected ? analysis.value?.estimates.get(selected.id) : undefined} detail={detail} loading={detailLoading} onClose={() => setSelectedId(null)} />
    </main>
  </div>;
}
