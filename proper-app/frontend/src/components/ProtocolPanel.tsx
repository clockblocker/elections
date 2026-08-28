import type { Point, PointEstimate, ProtocolDetail } from "../types";

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const percent = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
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
    <div className="point-metrics"><div><span>Turnout</span><strong>{percent.format(point.turnout)}%</strong></div><div><span>Result</span><strong>{percent.format(point.result)}%</strong></div><div><span>Party votes</span><strong>{number.format(point.optionVotes)}</strong></div></div>
    {estimate && <section className="estimate-card"><span>Model estimate · {estimate.baselineSource}</span><div><strong>{number.format(estimate.excessVotes)}</strong> positive deviation votes</div><small>Expected {number.format(estimate.expectedVotes)} from reference odds</small></section>}
    {loading && <div className="panel-loading">Loading preserved protocol…</div>}
    {detail && <>
      <section><h3>Ballot accounting</h3><dl className="accounting">{Object.entries(detail.accounting).map(([key, value]) => <div key={key}><dt>{labels[key] ?? key}</dt><dd>{number.format(value)}</dd></div>)}</dl></section>
      <section><h3>Complete result</h3><div className="vote-list">{detail.votes.map((vote) => <div key={vote.position}><i style={{ background: vote.color }} /><span>{vote.shortName}</span><strong>{number.format(vote.votes)}</strong><small>{vote.result == null ? "—" : `${percent.format(vote.result)}%`}</small></div>)}</div></section>
      <section><h3>Evidence</h3><dl className="source-meta"><div><dt>UIK TVD</dt><dd>{detail.precinct.uikTvd}</dd></div><div><dt>TIK TVD</dt><dd>{detail.precinct.tikTvd}</dd></div><div><dt>Derivation</dt><dd>{detail.source.derivation ?? "direct"}</dd></div><div><dt>Provenance</dt><dd>{detail.source.provenance}</dd></div></dl><a className="source-link" href={detail.source.finalUrl ?? detail.source.url} target="_blank" rel="noreferrer">Open official source ↗</a><code className="checksum">SHA-256 {detail.source.sha256}</code></section>
    </>}
  </aside>;
}
