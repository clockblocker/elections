import type { AnalyticalState, FilterMetadata, MatchStatus } from "../types";

interface Props {
  state: AnalyticalState;
  metadata: FilterMetadata;
  onChange(next: AnalyticalState): void;
  onReset(): void;
}

const toggle = (values: string[], value: string) => values.includes(value) ? values.filter((x) => x !== value) : [...values, value];

export function Filters({ state, metadata, onChange, onReset }: Props) {
  const setRange = (key: "turnoutMin" | "turnoutMax" | "resultMin" | "resultMax", value: string) => onChange({ ...state, [key]: Number(value) });
  return (
    <aside className="filters" aria-label="Analysis filters">
      <div className="panel-heading">
        <div><span className="eyebrow">Query</span><h2>Field parameters</h2></div>
        <button className="text-button" onClick={onReset}>Clear</button>
      </div>

      <fieldset>
        <legend>Party result</legend>
        <div className="choice-list">
          {metadata.parties.map((party) => <label className="check-row" key={party.id}>
            <input type="checkbox" checked={state.partyIds.includes(party.id)} onChange={() => onChange({ ...state, partyIds: toggle(state.partyIds, party.id) })} />
            <span className="swatch" style={{ background: party.color }} aria-hidden="true" /><span>{party.name}</span>
          </label>)}
        </div>
      </fieldset>

      <FilterSelect label="Region" value={state.regionIds[0] || ""} options={metadata.regions} onChange={(value) => onChange({ ...state, regionIds: value ? [value] : [], tikIds: [] })} />
      <FilterSelect label="Territorial commission (TIK)" value={state.tikIds[0] || ""} options={metadata.tiks} onChange={(value) => onChange({ ...state, tikIds: value ? [value] : [] })} />

      <fieldset>
        <legend>Precinct type</legend>
        <label className="check-row"><input type="checkbox" checked={!state.specialTypes.length} onChange={() => onChange({ ...state, specialTypes: [] })} /><span>All precincts</span></label>
        {metadata.specialTypes.map((type) => <label className="check-row" key={type.id}><input type="checkbox" checked={state.specialTypes.includes(type.id)} onChange={() => onChange({ ...state, specialTypes: toggle(state.specialTypes, type.id) })} /><span>{type.name}</span></label>)}
      </fieldset>

      <fieldset>
        <legend>Metadata match</legend>
        <div className="segmented">
          {metadata.matchStatuses.map((status) => <label key={status}><input type="checkbox" checked={state.matchStatuses.includes(status)} onChange={() => onChange({ ...state, matchStatuses: toggle(state.matchStatuses, status) as MatchStatus[] })} /><span>{status}</span></label>)}
        </div>
      </fieldset>

      <fieldset className="range-group"><legend>Turnout, %</legend><div className="range-pair">
        <label><span>From</span><input aria-label="Minimum turnout" type="number" min="0" max="100" value={state.turnoutMin} onChange={(e) => setRange("turnoutMin", e.target.value)} /></label>
        <span aria-hidden="true">—</span>
        <label><span>To</span><input aria-label="Maximum turnout" type="number" min="0" max="100" value={state.turnoutMax} onChange={(e) => setRange("turnoutMax", e.target.value)} /></label>
      </div></fieldset>

      <fieldset className="range-group"><legend>Party result, %</legend><div className="range-pair">
        <label><span>From</span><input aria-label="Minimum result" type="number" min="0" max="100" value={state.resultMin} onChange={(e) => setRange("resultMin", e.target.value)} /></label>
        <span aria-hidden="true">—</span>
        <label><span>To</span><input aria-label="Maximum result" type="number" min="0" max="100" value={state.resultMax} onChange={(e) => setRange("resultMax", e.target.value)} /></label>
      </div></fieldset>
    </aside>
  );
}

function FilterSelect({ label, value, options, onChange }: { label: string; value: string; options: Array<{ id: string; name: string; count?: number }>; onChange(value: string): void }) {
  return <label className="select-field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}><option value="">All</option>{options.map((option) => <option value={option.id} key={option.id}>{option.name}{option.count === undefined ? "" : ` · ${option.count}`}</option>)}</select></label>;
}
