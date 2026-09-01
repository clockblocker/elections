import { useEffect, useMemo, useState } from "react";
import { DEFAULT_PARAMETERS, analyze, summarizeAnalysis } from "./analysis";
import { api } from "./api";
import { ProtocolPanel } from "./components/ProtocolPanel";
import { Scatterplot } from "./components/Scatterplot";
import type { AnalysisParameters, BallotOption, ElectionSummary, Metadata, Point, ProtocolDetail } from "./types";
import "./styles.css";

const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });
const thresholdPercent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function finiteOr(value: string | null, fallback: number): number {
  const parsed = Number(value);
  return value !== null && Number.isFinite(parsed) ? parsed : fallback;
}

function initialQuery() {
  const query = new URLSearchParams(window.location.search);
  return {
    election: query.get("election") || "2021-duma",
    option: Number(query.get("option") ?? query.get("party")) || null,
    region: query.get("region") || null,
    parameters: {
      ...DEFAULT_PARAMETERS,
      coreFraction: finiteOr(query.get("core"), DEFAULT_PARAMETERS.coreFraction),
      reviewThreshold: finiteOr(query.get("review"), DEFAULT_PARAMETERS.reviewThreshold)
    } satisfies AnalysisParameters,
    selected: query.get("uik")
  };
}

function leadingOption(options: BallotOption[]): number | null {
  return options.reduce<BallotOption | null>((leading, option) =>
    !leading || Number(option.votes) > Number(leading.votes) ? option : leading, null)?.id ?? null;
}

function electionLabel(election: ElectionSummary): string {
  const year = election.electionDate.slice(0, 4);
  return election.ballot.kind === "party-list" ? `State Duma · ${year}` : `President · ${year}`;
}

export default function App() {
  const initial = useMemo(initialQuery, []);
  const [elections, setElections] = useState<ElectionSummary[]>([]);
  const [electionSlug, setElectionSlug] = useState(initial.election);
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
    const controller = new AbortController();
    api.elections(controller.signal).then((values) => {
      setElections(values);
      setElectionSlug((current) => values.some((item) => item.slug === current) ? current : values[0]?.slug ?? current);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load elections");
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setMetadata(null); setPoints([]); setDetail(null);
    api.metadata(electionSlug, controller.signal).then((value) => {
      setMetadata(value);
      setOptionId((current) => value.options.some((option) => option.id === current)
        ? current : leadingOption(value.options));
      setRegion((current) => {
        if (!current) return null;
        const exact = value.regions.find((item) => item.key === current);
        if (exact) return exact.key;
        const legacy = value.regions.filter((item) => item.code === current);
        return legacy.length === 1 ? legacy[0].key : null;
      });
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load metadata");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [electionSlug]);

  // Always fit the same national family. Geography is a display slice and must not
  // change an individual protocol's baseline, calibration percentile, or q-value.
  useEffect(() => {
    if (!optionId || !metadata) return;
    const controller = new AbortController(); setLoading(true); setError(null); setPoints([]); setDetail(null);
    api.points(electionSlug, optionId, null, controller.signal).then((next) => {
      setPoints(next);
      setSelectedId((current) => current && next.some((point) => point.id === current) ? current : null);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load points");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [electionSlug, metadata, optionId]);

  const visiblePoints = useMemo(
    () => region ? points.filter((point) => point.regionKey === region) : points,
    [points, region]
  );
  const selected = useMemo(
    () => visiblePoints.find((point) => point.id === selectedId) ?? null,
    [visiblePoints, selectedId]
  );
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    const controller = new AbortController(); setDetailLoading(true); setDetail(null);
    api.protocol(electionSlug, selected.id, controller.signal).then(setDetail)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setNotice(reason instanceof Error ? reason.message : "Could not load protocol");
      }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [electionSlug, selected]);

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
  const option = metadata?.options.find((item) => item.id === optionId) ?? null;
  const election = elections.find((item) => item.slug === electionSlug) ?? null;
  const year = metadata?.election.electionDate.slice(0, 4) ?? electionSlug.slice(0, 4);
  const targetNoun = metadata?.ballot.kind === "presidential" ? "candidate" : "party";
  const hasDegDataset = metadata?.coverage.degPolicy === "excluded-outside-protocol-cloud-model";

  useEffect(() => {
    if (!optionId) return;
    const query = new URLSearchParams({
      election: electionSlug, option: String(optionId), core: String(parameters.coreFraction),
      review: String(parameters.reviewThreshold)
    });
    if (region) query.set("region", region); if (selectedId) query.set("uik", selectedId);
    window.history.replaceState(null, "", `${window.location.pathname}?${query}`);
  }, [electionSlug, optionId, parameters, region, selectedId]);

  const chooseElection = (slug: string) => {
    setElectionSlug(slug); setOptionId(null); setRegion(null); setSelectedId(null); setDetail(null);
  };
  const updateParameter = (key: keyof AnalysisParameters, value: number) => {
    setParameters((current) => ({ ...current, [key]: value }));
  };
  const copyLink = async () => {
    await navigator.clipboard.writeText(window.location.href); setNotice("Shareable analysis URL copied");
    window.setTimeout(() => setNotice(null), 2200);
  };
  const exportAnalysis = () => {
    if (!analysis.value || !option || !summary || !metadata) return;
    const visibleIds = new Set(visiblePoints.map((point) => point.id));
    const estimates = Object.fromEntries(
      [...analysis.value.estimates].filter(([id]) => visibleIds.has(id))
    );
    const result = {
      election: electionSlug, scope: `physical-uik-${metadata.ballot.kind}`, degIncluded: false,
      geography: region ?? "all", option: { id: option.id, position: option.position, name: option.name },
      method: analysis.value.method, parameters, modeledPopulation: "all physical UIK protocols",
      pSusMeaning: "incompatibility with the robust election-wide protocol core; not probability of fraud",
      multipleTestingDiagnostic: "Benjamini-Yekutieli q-value from the bivariate core tail",
      core: analysis.value.core, summary, estimates
    };
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
    anchor.download = `${electionSlug}-${option.shortName.replace(/\s+/g, "-").toLowerCase()}-protocol-cloud-v3.json`;
    anchor.click(); URL.revokeObjectURL(anchor.href);
  };

  if (!metadata && loading) return <main className="state-page"><div className="state-mark">{year.slice(2)}</div><span className="eyebrow">Loading physical evidence</span><h1>Preparing the precinct field</h1><p>Connecting to PostgreSQL and resolving the {year} protocol index…</p></main>;
  if (!metadata || (error && !points.length)) return <main className="state-page error"><div className="state-mark">!</div><span className="eyebrow">Workbench unavailable</span><h1>The evidence API did not answer</h1><p>{error ?? "No metadata was returned."}</p><button onClick={() => window.location.reload()}>Retry</button></main>;

  return <div className="app-shell">
    <header className="masthead">
      <a className="brand" href="/"><span className="brand-year">{year}</span><span><strong>Physical Vote Field</strong><small>{metadata.ballot.kind === "party-list" ? "State Duma" : "Presidential election"} research edition</small></span></a>
      <div className="scope-ribbon"><i /> Physical UIKs only <span>{hasDegDataset ? "DEG outside model" : "nationwide ballot field"}</span></div>
      <div className="header-actions"><button onClick={copyLink}>Copy analysis link</button><button className="primary" onClick={exportAnalysis} disabled={!analysis.value}>Export result</button></div>
    </header>
    {notice && <div className="toast" role="status">{notice}</div>}
    <main className="workbench">
      <aside className="control-panel">
        <div className="panel-title"><span className="panel-index">01</span><div><span className="eyebrow">Model controls</span><h1>Protocol-field parameters</h1></div></div>
        <label className="field"><span>Election</span><select value={electionSlug} onChange={(event) => chooseElection(event.target.value)}>{elections.map((item) => <option value={item.slug} key={item.slug}>{electionLabel(item)}</option>)}</select></label>
        <label className="field"><span>Ballot</span><select disabled><option>{metadata.ballot.name}</option></select></label>
        <label className="field"><span>Target {targetNoun}</span><select value={optionId ?? ""} onChange={(event) => setOptionId(Number(event.target.value))}>{metadata.options.map((item) => <option value={item.id} key={item.id}>{item.position}. {item.shortName}</option>)}</select></label>
        <label className="field"><span>Display geography</span><select value={region ?? ""} onChange={(event) => { setRegion(event.target.value || null); setSelectedId(null); }}><option value="">All {metadata.coverage.regions} regions</option>{metadata.regions.map((item) => <option value={item.key} key={item.key}>{item.code} · {item.name} · {integer.format(item.precincts)}</option>)}</select></label>

        <fieldset className="parameter-group"><legend>Robust central field</legend><p>Fit the expected bivariate distribution to the densest central share of all {year} physical UIK protocols.</p><div className="threshold"><input aria-label="Core protocol fraction" type="range" min="0.35" max="0.7" step="0.05" value={parameters.coreFraction} onChange={(event) => updateParameter("coreFraction", Number(event.target.value))} /><output>{percent.format(parameters.coreFraction)}</output></div></fieldset>
        <label className="field parameter-select"><span>P_sus review threshold</span><select value={parameters.reviewThreshold} onChange={(event) => updateParameter("reviewThreshold", Number(event.target.value))}><option value="0.95">P1 · 95%</option><option value="0.99">P2 · 99%</option><option value="0.999">P3 · 99.9% default</option></select></label>

        <section className="scope-note neutral-note"><span className="eyebrow">What P_sus means</span><h2>Distance from the election core</h2><p>P_sus grades each complete UIK protocol against the robust turnout × result distribution fitted to all physical UIKs. Dense high-high tails remain deviations; they are not learned as local normality. It is not a probability of fraud.</p></section>
        <section className="scope-note"><span className="eyebrow">Analysis boundary</span><h2>{hasDegDataset ? "Why DEG is absent" : "Physical protocols only"}</h2><p>{hasDegDataset ? "Electronic results are aggregate returns without the physical precinct distribution this model compares. They remain outside every fit and denominator." : "This nationwide field uses complete physical UIK protocols. Missing official-source protocols remain visible in coverage and are never imputed."}</p></section>
        <section className="coverage"><span>{metadata.coverage.importedProtocols.toLocaleString()} imported protocols</span><span>{metadata.coverage.missingProtocols} source gaps retained</span><span>{metadata.coverage.tiks.toLocaleString()} TIKs</span></section>
      </aside>

      <section className="analysis-column">
        <div className="analysis-heading"><div><span className="eyebrow">02 · Election-wide protocol screen</span><h1>{option?.shortName ?? "Option"} turnout × result field</h1><p>{region ? `${metadata.regions.find((item) => item.key === region)?.name} · scores fixed to the all-UIK model` : `Russian Federation · robust core of ${analysis.value?.core.protocols.toLocaleString() ?? "all"} protocols`}</p></div>{loading && <span className="loading-pill">Loading protocols…</span>}</div>
        {analysis.error && <div className="analysis-error">{analysis.error}</div>}
        {summary && <div className="metric-row">
          <article><span>P_sus review queue</span><strong>{integer.format(summary.flaggedProtocols)}</strong><small>score ≥ {thresholdPercent.format(parameters.reviewThreshold)}</small></article>
          <article><span>Observed / core expectation</span><strong>{compact.format(summary.observedVotes)} / {compact.format(summary.expectedVotes)}</strong><small>target-{targetNoun} votes</small></article>
          <article><span>Flagged residual</span><strong>{integer.format(summary.flaggedResidualVotes)}</strong><small>actual − expected votes</small></article>
          <article><span>Model coverage</span><strong>{integer.format(summary.scoredProtocols)} / {integer.format(summary.protocols)}</strong><small>{summary.unscoredProtocols} explicitly unscored</small></article>
        </div>}
        {visiblePoints.length && option && analysis.value ? <Scatterplot points={visiblePoints} estimates={analysis.value.estimates} coreContour50={analysis.value.core.contour50} coreContour95={analysis.value.core.contour95} selectedId={selectedId} onSelect={(point) => setSelectedId(point.id)} /> : !loading && <div className="empty-chart">No physical UIK points match this selection.</div>}
        {reviewQueue.length > 0 && <section className="review-queue"><div><span className="eyebrow">Highest election-core incompatibility</span><h2>Protocol review queue</h2></div><div className="review-items">{reviewQueue.map(({ point, estimate }) => <button key={point.id} onClick={() => setSelectedId(point.id)} className={selectedId === point.id ? "selected" : ""}><span><b>UIK {point.uikNumber}</b><small>{point.regionName}</small></span><strong>{estimate?.grade}</strong><em>{percent.format(estimate?.pSus ?? 0)}</em></button>)}</div></section>}
        <section className="method-strip"><div><span className="eyebrow">Method note</span><h2>Protocol-cloud CLT · v3</h2></div><p>Each UIK protocol is one observation. A robust bivariate Gaussian core is fitted on continuity-corrected turnout and selected-option result logits across the complete election. P_sus is the chi-square incompatibility of the actual protocol with that core, including finite-protocol sampling variance. The fit is deliberately not conditioned on turnout, TIK, or region, so a coordinated high-turnout/high-result tail remains visible. Aggregate returns alone still cannot identify fraud.</p><code>protocol-cloud-clt-v3 · all physical UIKs · DEG=false</code></section>
      </section>

      <ProtocolPanel point={selected} estimate={selected ? analysis.value?.estimates.get(selected.id) : undefined} detail={detail} loading={detailLoading} onClose={() => setSelectedId(null)} />
    </main>
  </div>;
}
