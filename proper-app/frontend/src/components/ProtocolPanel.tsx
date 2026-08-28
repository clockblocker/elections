import type { Point, PointEstimate, ProtocolDetail } from "../types";

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const percent = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const score = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 });
const labels: Record<string, string> = {
  registeredVoters: "Registered voters", ballotsReceived: "Ballots received",
  ballotsIssuedEarly: "Issued early", ballotsIssuedAtStation: "Issued at station",
  ballotsIssuedOutside: "Issued outside station", ballotsCancelled: "Cancelled ballots",
  portableBoxBallots: "Portable-box ballots", stationaryBoxBallots: "Stationary-box ballots",
  invalidBallots: "Invalid ballots", validBallots: "Valid ballots",
  lostBallots: "Lost ballots", unaccountedBallots: "Unaccounted ballots"
};

export function ProtocolPanel({ point, estimate, detail, loading, onClose }: {
  point: Point | null;
  estimate?: PointEstimate;
  detail: ProtocolDetail | null;
  loading: boolean;
  onClose(): void;
}) {
  if (!point) return <aside className="protocol-panel empty-panel">
    <span className="panel-index">03</span><div><span className="eyebrow">Protocol evidence</span><h2>Select a dot</h2><p>Click a precinct to inspect its accounting, complete party result, and official source fingerprint.</p></div>
  </aside>;
  return <aside className="protocol-panel">
    <header><div><span className="eyebrow">Pinned physical precinct</span><h2>UIK {point.uikNumber}</h2><p>{point.regionName}<br />{point.tikName}</p></div><button className="close" onClick={onClose} aria-label="Close protocol">×</button></header>
    <div className="point-metrics"><div><span>Turnout</span><strong>{point.turnout === null ? "—" : `${percent.format(point.turnout)}%`}</strong></div><div><span>Result</span><strong>{point.result === null ? "—" : `${percent.format(point.result)}%`}</strong></div><div><span>Party votes</span><strong>{number.format(point.optionVotes)}</strong></div></div>
    {estimate?.status === "scored" && <section className="estimate-card clt-card">
      <div className="score-line"><span><small>P_sus grade</small><strong>{estimate.grade}</strong></span><b>{score.format(estimate.pSus ?? 0)}</b></div>
      <p>Election-wide protocol-core incompatibility—not a probability of fraud.</p>
      <div className="actual-expected"><span><small>Actual</small><strong>{number.format(estimate.observedVotes)}</strong></span><i>vs</i><span><small>Expected</small><strong>{number.format(estimate.expectedVotes ?? 0)}</strong></span></div>
      <dl className="model-diagnostics">
        <div><dt>95% predictive interval</dt><dd>{number.format(estimate.interval95?.[0] ?? 0)}–{number.format(estimate.interval95?.[1] ?? 0)}</dd></div>
        <div><dt>Residual</dt><dd>{(estimate.residualVotes ?? 0) >= 0 ? "+" : ""}{number.format(estimate.residualVotes ?? 0)} votes</dd></div>
        <div><dt>Core expected turnout</dt><dd>{percent.format(estimate.expectedTurnout ?? 0)}%</dd></div>
        <div><dt>Result-axis residual</dt><dd>{estimate.zScore?.toFixed(2)} z</dd></div>
        <div><dt>Bivariate distance</dt><dd>{estimate.mahalanobisSquared?.toFixed(2)} D²</dd></div>
        <div><dt>Direction</dt><dd>{estimate.direction}</dd></div>
        <div><dt>Core-tail p</dt><dd>{(estimate.pValue ?? 1) < 0.0001 ? (estimate.pValue ?? 1).toExponential(2) : (estimate.pValue ?? 1).toFixed(4)}</dd></div>
        <div><dt>BY q · diagnostic</dt><dd>{(estimate.qValue ?? 1) < 0.0001 ? (estimate.qValue ?? 1).toExponential(2) : (estimate.qValue ?? 1).toFixed(4)}</dd></div>
        <div><dt>Field / count variance</dt><dd>{estimate.overdispersion?.toFixed(1)}×</dd></div>
        <div><dt>Null model</dt><dd>{estimate.baselineSource}</dd></div>
        <div><dt>Robust core</dt><dd>{number.format(estimate.peerPrecincts)} UIKs</dd></div>
      </dl>
      {estimate.qualityFlags.length > 0 && <div className="quality-flags">{estimate.qualityFlags.map((flag) => <span key={flag}>{flag}</span>)}</div>}
    </section>}
    {estimate?.status === "unscored" && <section className="estimate-card unscored-card"><span>P_sus grade · U</span><div><strong>Not scored</strong></div><small>{estimate.reason?.replaceAll("-", " ")}. The protocol remains in coverage and export.</small>{estimate.expectedVotes !== null && <small>Peer expectation was {number.format(estimate.expectedVotes)}, but the CLT success/failure-count gate was not met.</small>}</section>}
    {loading && <div className="panel-loading">Loading preserved protocol…</div>}
    {detail && <>
      <section><h3>Ballot accounting</h3><dl className="accounting">{Object.entries(detail.accounting).map(([key, value]) => <div key={key}><dt>{labels[key] ?? key}</dt><dd>{number.format(value)}</dd></div>)}</dl></section>
      <section><h3>Complete result</h3><div className="vote-list">{detail.votes.map((vote) => <div key={vote.position}><i style={{ background: vote.color }} /><span>{vote.shortName}</span><strong>{number.format(vote.votes)}</strong><small>{vote.result == null ? "—" : `${percent.format(vote.result)}%`}</small></div>)}</div></section>
      <section><h3>Evidence</h3><dl className="source-meta"><div><dt>UIK TVD</dt><dd>{detail.precinct.uikTvd}</dd></div><div><dt>TIK TVD</dt><dd>{detail.precinct.tikTvd}</dd></div><div><dt>Derivation</dt><dd>{detail.source.derivation ?? "direct"}</dd></div><div><dt>Provenance</dt><dd>{detail.source.provenance}</dd></div></dl><a className="source-link" href={detail.source.finalUrl ?? detail.source.url} target="_blank" rel="noreferrer">Open official source ↗</a><code className="checksum">SHA-256 {detail.source.sha256}</code></section>
    </>}
  </aside>;
}
