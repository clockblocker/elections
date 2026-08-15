import type { AnalyticalState, BallotKind, DatasetStatus, FilterMetadata, MatchStatus, Party, Point, PrecinctDetail, SourceLink } from "./types";

const configuredOrigin = import.meta.env.VITE_API_URL?.trim().replace(/\/$/, "") || "";
export const API_BASE = `${configuredOrigin}/api/v1`;

export class ApiError extends Error {
  constructor(message: string, public readonly status?: number) { super(message); this.name = "ApiError"; }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = ((await response.json()) as { detail?: string }).detail || detail; } catch { /* not JSON */ }
    throw new ApiError(detail || `Request failed (${response.status})`, response.status);
  }
  return response.json() as Promise<T>;
}

type DatasetStatusWire = { election_slug: string | null; election_name: string | null; ingestion_status: string; ready: boolean; reconciled: boolean; imported_at: string | null; result_records: number; validation_errors: number; latest_source_key: string | null; latest_source_sha256: string | null };
type PartyWire = { id: number; name: string; short_name: string | null; position: number | null };
type BallotWire = { id: number; kind: BallotKind; name: string; scope_key: string; oik_id: number | null };
type DistrictWire = { id: number; code: string; name: string; region_name: string | null; ballot_id: number; candidate_count: number; result_records: number };
type CandidateWire = { id: number; ballot_id: number; oik_id: number; district_code: string; position: number; full_name: string; party_affiliation: string | null; is_winner: boolean };
type AffiliationWire = { value: string; candidates: number };
type RegionWire = { id: number | null; name: string; code: string | null; result_records: number };
type TikWire = { id: number | null; name: string; region_name: string; number: string | null; result_records: number };
type SpecialWire = { value: string; label: string; result_records: number; is_deg: boolean };
type PointWire = { result_record_id: number; ballot_id: number | null; ballot_kind: BallotKind; scope_key: string; oik_id: number | null; party_id: number | null; candidate_id: number | null; candidate_name: string | null; party_affiliation: string | null; is_winner: boolean; uik_number: string; tik_name: string | null; region_name: string; registered_voters: number; ballots_counted: number; party_votes: number; turnout_percent: number | null; party_percent: number | null; match_status: string; validation_status: string | null; special_type: string | null; is_deg: boolean; flags: string[] };
type PageWire = { items: PointWire[]; offset: number; limit: number; total: number; has_more: boolean };
type MemberWire = { full_name: string; role: string | null; nominator: string | null };
type PartyResultWire = { party_id: number; name: string; votes: number; percent: number | null };
type CandidateResultWire = { candidate_id: number; full_name: string; party_affiliation: string | null; votes: number; percent: number | null; is_winner: boolean };
type SourceWire = { label: string; url: string; sha256: string | null };
type ProtocolWire = { result_record_id: number; ballot_kind: BallotKind; ballot_name: string; turnout_percent: number | null; party_results: PartyResultWire[]; candidate_results: CandidateResultWire[]; sources: SourceWire[] };
type DetailWire = { result_record_id: number; uik_number: string; hierarchy: { election: string; ballot: string; region: string; district: string | null; tik: string | null; uik: string }; accounting: Record<string, number | null>; turnout_percent: number | null; party_results: PartyResultWire[]; candidate_results: CandidateResultWire[]; protocols: ProtocolWire[]; commission: { address: string | null; chairperson: MemberWire | null; members: MemberWire[] } | null; match_status: string; special_type: string | null; is_deg: boolean; flags: string[]; validation_findings: Array<{ code: string; severity: string; expected: string | null; actual: string | null }>; sources: SourceWire[] };

const palette = ["#d95d39", "#972d37", "#2f6690", "#5b8e7d", "#725ac1", "#d19c1d", "#347474", "#ba5c8e"];
const normalizeMatch = (value: string): MatchStatus => value === "matched" || value === "partial" ? value : "unmatched";
const camel = (value: string) => value.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());

function queryFor(state: AnalyticalState, metadata: FilterMetadata): URLSearchParams {
  const params = new URLSearchParams();
  params.append("ballot_kind", state.ballotKind);
  if (state.ballotId) params.append("ballot_id", state.ballotId);
  if (state.districtId) params.append("oik_id", state.districtId);
  if (state.candidateId) params.append("candidate_id", state.candidateId);
  state.affiliations.forEach((value) => params.append("affiliation", value));
  if (state.winner !== null) params.set("winner", String(state.winner));
  const partyById = new Map(metadata.parties.map((party) => [party.id, party]));
  if (state.ballotKind === "party_list") state.partyIds.forEach((value) => { const id = Number(value); if (Number.isInteger(id) && partyById.has(value)) params.append("party_id", String(id)); });
  state.regionIds.forEach((value) => params.append("region", value));
  state.tikIds.forEach((value) => params.append("tik", value));
  state.specialTypes.forEach((value) => params.append("special_type", value));
  state.matchStatuses.forEach((value) => params.append("match_status", value));
  if (state.turnoutMin > 0) params.set("turnout_min", String(state.turnoutMin)); if (state.turnoutMax < 100) params.set("turnout_max", String(state.turnoutMax));
  if (state.resultMin > 0) params.set("result_min", String(state.resultMin)); if (state.resultMax < 100) params.set("result_max", String(state.resultMax));
  params.set("limit", "20000");
  return params;
}

export interface ApiClient {
  status(signal?: AbortSignal): Promise<DatasetStatus>;
  filters(signal?: AbortSignal): Promise<FilterMetadata>;
  points(state: AnalyticalState, metadata: FilterMetadata, signal?: AbortSignal): Promise<Point[]>;
  precinct(resultRecordId: string, signal?: AbortSignal): Promise<PrecinctDetail>;
}

export const api: ApiClient = {
  async status(signal) {
    const wire = await get<DatasetStatusWire>("/dataset/status", signal);
    return { ready: wire.ready, reconciled: wire.reconciled, pointCount: wire.result_records, sourceVersion: wire.latest_source_key || wire.latest_source_sha256?.slice(0, 12) || wire.election_slug || "unversioned", updatedAt: wire.imported_at || undefined };
  },
  async filters(signal) {
    const [parties, ballots, districts, candidates, affiliations, regions, tiks, special] = await Promise.all([get<PartyWire[]>("/parties", signal), get<BallotWire[]>("/ballots", signal), get<DistrictWire[]>("/districts", signal), get<CandidateWire[]>("/candidates", signal), get<AffiliationWire[]>("/affiliations", signal), get<RegionWire[]>("/regions", signal), get<TikWire[]>("/tiks", signal), get<SpecialWire[]>("/special-types", signal)]);
    const normalizedParties: Party[] = parties.map((party, index) => ({ id: String(party.id), name: party.name, shortName: party.short_name || party.name, color: palette[index % palette.length] }));
    return { parties: normalizedParties, ballots: ballots.map((ballot) => ({ id: String(ballot.id), kind: ballot.kind, name: ballot.name, scopeKey: ballot.scope_key, districtId: ballot.oik_id ? String(ballot.oik_id) : undefined })), districts: districts.map((district) => ({ id: String(district.id), code: district.code, name: `${district.code} · ${district.name}`, regionName: district.region_name || undefined, ballotId: String(district.ballot_id), count: district.result_records })), candidates: candidates.map((candidate) => ({ id: String(candidate.id), name: candidate.full_name, ballotId: String(candidate.ballot_id), districtId: String(candidate.oik_id), affiliation: candidate.party_affiliation || undefined, winner: candidate.is_winner, position: candidate.position })), affiliations: affiliations.map((item) => ({ id: item.value, name: item.value, count: item.candidates })), regions: regions.map((region) => ({ id: region.name, name: region.name, count: region.result_records })), tiks: tiks.map((tik) => ({ id: tik.name, name: `${tik.name} · ${tik.region_name}`, count: tik.result_records })), specialTypes: special.map((item) => ({ id: item.value, name: item.label, count: item.result_records })), matchStatuses: ["matched", "partial", "unmatched"], sourceVersion: "pending-status" };
  },
  async points(state, metadata, signal) {
    const params = queryFor(state, metadata); const wires: PointWire[] = [];
    for (let offset = 0; ; offset += 20000) {
      params.set("offset", String(offset)); const page = await get<PageWire>(`/points?${params}`, signal); wires.push(...page.items); if (!page.has_more) break;
    }
    const parties = new Map(metadata.parties.map((party) => [party.id, party]));
    return wires.map((wire) => {
      const candidateKey = wire.candidate_id == null ? null : `candidate:${wire.candidate_id}`; const party = wire.party_id == null ? null : parties.get(String(wire.party_id)); const uikId = String(wire.result_record_id); const seriesId = candidateKey || String(wire.party_id);
      const missingFlags = [wire.turnout_percent === null ? "missing_turnout" : "", wire.party_percent === null ? "missing_result" : ""].filter(Boolean);
      return { id: `${uikId}:${seriesId}`, uikId, uikNumber: wire.uik_number, tikId: wire.tik_name || "", tikName: wire.tik_name || "TIK not recorded", regionId: wire.region_name, regionName: wire.region_name, partyId: seriesId, partyName: wire.candidate_name || party?.name || `Option ${seriesId}`, ballotId: String(wire.ballot_id || ""), ballotKind: wire.ballot_kind || "party_list", districtId: wire.oik_id == null ? null : String(wire.oik_id), candidateId: wire.candidate_id == null ? null : String(wire.candidate_id), affiliation: wire.party_affiliation || null, winner: wire.is_winner || false, registeredVoters: wire.registered_voters, ballotsIssued: wire.ballots_counted, turnout: wire.turnout_percent ?? 0, partyVotes: wire.party_votes, partyShare: wire.party_percent ?? 0, specialFlags: [...wire.flags, ...missingFlags, ...(wire.special_type ? [wire.special_type] : []), ...(wire.is_deg ? ["deg"] : [])].filter((value, index, all) => all.indexOf(value) === index), matchStatus: normalizeMatch(wire.match_status), validationStatus: wire.validation_status };
    });
  },
  async precinct(resultRecordId, signal) {
    const wire = await get<DetailWire>(`/uiks/${encodeURIComponent(resultRecordId)}`, signal);
    const member = (value: MemberWire) => ({ name: value.full_name, role: value.role || undefined, nominator: value.nominator });
    const source = (value: SourceWire): SourceLink => ({ label: value.label, url: value.url, checksum: value.sha256 || undefined });
    const partyResults = (values: PartyResultWire[]) => values.map((result) => ({ partyId: String(result.party_id), partyName: result.name, votes: result.votes, share: result.percent || 0 }));
    const candidateResults = (values: CandidateResultWire[]) => values.map((result) => ({ candidateId: String(result.candidate_id), candidateName: result.full_name, affiliation: result.party_affiliation, votes: result.votes, share: result.percent || 0, winner: result.is_winner }));
    return { id: String(wire.result_record_id), uikNumber: wire.uik_number, tikName: wire.hierarchy.tik || "TIK not recorded", regionName: wire.hierarchy.region, districtName: wire.hierarchy.district, address: wire.commission?.address, chairperson: wire.commission?.chairperson ? member(wire.commission.chairperson) : null, members: wire.commission?.members.map(member) || null, accounting: Object.fromEntries(Object.entries(wire.accounting).map(([key, value]) => [camel(key), value])), partyResults: partyResults(wire.party_results), candidateResults: candidateResults(wire.candidate_results), protocols: wire.protocols.map((protocol) => ({ resultRecordId: String(protocol.result_record_id), ballotKind: protocol.ballot_kind, ballotName: protocol.ballot_name, turnout: protocol.turnout_percent, partyResults: partyResults(protocol.party_results), candidateResults: candidateResults(protocol.candidate_results), sources: protocol.sources.map(source) })), specialFlags: [...wire.flags, ...(wire.special_type ? [wire.special_type] : []), ...(wire.is_deg ? ["deg"] : [])], matchStatus: normalizeMatch(wire.match_status), validationMessages: wire.validation_findings.map((finding) => `${finding.severity}: ${finding.code}${finding.expected || finding.actual ? ` (expected ${finding.expected || "—"}, actual ${finding.actual || "—"})` : ""}`), sources: wire.sources.map(source) };
  },
};
