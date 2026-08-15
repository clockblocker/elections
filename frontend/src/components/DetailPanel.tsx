import type { Point, PrecinctDetail } from "../types";

export function DetailPanel({ point, detail, loading, error, onClose }: { point: Point | null; detail: PrecinctDetail | null; loading: boolean; error: string | null; onClose(): void }) {
  if (!point) return <aside className="detail-panel detail-empty"><span className="index-mark">03</span><div><span className="eyebrow">Evidence record</span><h2>Select a precinct</h2><p>Hover any mark for a summary, then click to pin its complete UIK record here.</p></div></aside>;
  return <aside className="detail-panel" aria-live="polite">
    <div className="detail-top"><div><span className="eyebrow">Pinned evidence</span><h2>UIK {point.uikNumber}</h2><p>{point.tikName} · {point.regionName}</p></div><button className="icon-button" aria-label="Close precinct details" onClick={onClose}>×</button></div>
    <div className="metric-strip"><Metric label="Turnout" value={`${point.turnout.toFixed(1)}%`} /><Metric label={point.partyName} value={`${point.partyShare.toFixed(1)}%`} /><Metric label="Votes" value={point.partyVotes.toLocaleString()} /></div>
    {loading && <div className="detail-loading"><span className="spinner" /> Loading linked records…</div>}
    {error && <div className="notice error">Could not load complete metadata: {error}</div>}
    {detail && <>
      <Section title="Hierarchy"><Description items={{ Region: detail.regionName, TIK: detail.tikName, District: detail.districtName || "Not recorded", Address: detail.address || "Missing from commission snapshot" }} /></Section>
      <Section title="Ballot accounting"><Description items={Object.fromEntries(Object.entries(detail.accounting).map(([key, value]) => [humanize(key), value === null ? "Missing" : value.toLocaleString()]))} /></Section>
      <Section title="Party results"><div className="result-table">{detail.partyResults.map((result) => <div key={result.partyId}><span>{result.partyName}</span><strong>{result.votes.toLocaleString()}</strong><span>{result.share.toFixed(1)}%</span></div>)}</div></Section>
      <Section title="Commission"><Description items={{ Chairperson: detail.chairperson?.name || "Missing commission information", Nominator: detail.chairperson?.nominator || "Not recorded", Membership: detail.members?.length ? `${detail.members.length} linked member record${detail.members.length === 1 ? "" : "s"}` : "Missing commission information" }} /></Section>
      <Section title="Validation"><div className={`status-badge ${detail.matchStatus}`}>{detail.matchStatus}</div>{detail.validationMessages?.map((message) => <p className="validation" key={message}>{message}</p>)}</Section>
      <Section title="Sources"><ul className="source-list">{detail.sources.map((source) => <li key={source.url}><a href={source.url} target="_blank" rel="noreferrer">{source.label} ↗</a>{source.checksum && <code>{source.checksum}</code>}</li>)}</ul></Section>
    </>}
  </aside>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><span>{label}</span><strong>{value}</strong></div>; }
function Section({ title, children }: React.PropsWithChildren<{ title: string }>) { return <section className="detail-section"><h3>{title}</h3>{children}</section>; }
function Description({ items }: { items: Record<string, string> }) { return <dl>{Object.entries(items).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>; }
const humanize = (value: string) => value.replace(/([A-Z])/g, " $1").replace(/^./, (c) => c.toUpperCase());
