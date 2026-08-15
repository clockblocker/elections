import type { AnalyticalState, DatasetStatus, FilterMetadata, Point, PrecinctDetail } from "./types";

export const demoMetadata: FilterMetadata = {
  parties: [
    { id: "united-russia", name: "United Russia", shortName: "UR", color: "#d95d39" },
    { id: "communist-party", name: "Communist Party", shortName: "CPRF", color: "#972d37" },
    { id: "ldpr", name: "Liberal Democratic Party", shortName: "LDPR", color: "#2f6690" },
    { id: "new-people", name: "New People", shortName: "NP", color: "#5b8e7d" },
  ],
  ballots: [{ id: "demo-party-list", kind: "party_list", name: "Federal party list", scopeKey: "federal" }],
  districts: [],
  candidates: [],
  affiliations: [],
  regions: [
    { id: "moscow", name: "Moscow" }, { id: "tatarstan", name: "Republic of Tatarstan" },
    { id: "novosibirsk", name: "Novosibirsk Oblast" }, { id: "perm", name: "Perm Krai" },
  ],
  tiks: Array.from({ length: 12 }, (_, i) => ({ id: `tik-${i + 1}`, name: `Territorial commission ${i + 1}` })),
  specialTypes: [{ id: "deg", name: "Remote electronic voting" }, { id: "temporary", name: "Temporary precinct" }, { id: "abroad", name: "Overseas precinct" }],
  matchStatuses: ["matched", "partial", "unmatched"],
  sourceVersion: "demo-2021.09",
};

export const demoStatus: DatasetStatus = { ready: true, reconciled: true, sourceVersion: demoMetadata.sourceVersion, pointCount: 4800 };

let seed = 20210919;
const random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);

const allDemoPoints: Point[] = Array.from({ length: 4800 }, (_, index) => {
  const region = demoMetadata.regions[index % demoMetadata.regions.length];
  const party = demoMetadata.parties[index % demoMetadata.parties.length];
  const turnout = Math.min(99.5, Math.max(13, 35 + random() * 48 + (index % 31 === 0 ? 25 : 0)));
  const base = party.id === "united-russia" ? 18 + turnout * 0.48 : party.id === "communist-party" ? 31 - turnout * 0.19 : 7 + turnout * 0.04;
  const partyShare = Math.min(98, Math.max(0.5, base + (random() - 0.5) * 18));
  const registeredVoters = 340 + Math.floor(random() * 2200);
  const ballotsIssued = Math.round(registeredVoters * turnout / 100);
  const uikId = `uik-${Math.floor(index / demoMetadata.parties.length) + 1}`;
  return {
    id: `${uikId}:${party.id}`,
    uikId,
    uikNumber: String(Math.floor(index / demoMetadata.parties.length) + 1),
    tikId: `tik-${(index % 12) + 1}`,
    tikName: `Territorial commission ${(index % 12) + 1}`,
    regionId: region.id,
    regionName: region.name,
    partyId: party.id,
    partyName: party.name,
    ballotId: "demo-party-list",
    ballotKind: "party_list",
    districtId: null,
    candidateId: null,
    affiliation: null,
    winner: false,
    registeredVoters,
    ballotsIssued,
    turnout,
    partyVotes: Math.round(ballotsIssued * partyShare / 100),
    partyShare,
    specialFlags: index % 97 === 0 ? ["deg"] : index % 71 === 0 ? ["temporary"] : [],
    matchStatus: index % 89 === 0 ? "unmatched" : index % 53 === 0 ? "partial" : "matched",
  };
});

export function demoPoints(state: AnalyticalState): Point[] {
  if (state.ballotKind === "single_member") return [];
  const parties = state.partyIds.length ? state.partyIds : demoMetadata.parties.map((x) => x.id);
  return allDemoPoints.filter((point) =>
    parties.includes(point.partyId) &&
    (!state.regionIds.length || state.regionIds.includes(point.regionId)) &&
    (!state.tikIds.length || state.tikIds.includes(point.tikId)) &&
    (!state.specialTypes.length || state.specialTypes.some((flag) => point.specialFlags.includes(flag))) &&
    (!state.matchStatuses.length || state.matchStatuses.includes(point.matchStatus)) &&
    point.turnout >= state.turnoutMin && point.turnout <= state.turnoutMax &&
    point.partyShare >= state.resultMin && point.partyShare <= state.resultMax
  );
}

export function demoDetail(uikId: string): PrecinctDetail {
  const points = allDemoPoints.filter((point) => point.uikId === uikId);
  const first = points[0] || allDemoPoints[0];
  return {
    id: first.uikId, uikNumber: first.uikNumber, tikName: first.tikName, regionName: first.regionName,
    districtName: "Federal party-list district", address: `School building, precinct ${first.uikNumber}`,
    chairperson: Number(first.uikNumber) % 11 === 0 ? null : { name: "Commission record available in source", role: "Chairperson", nominator: "Local assembly" },
    members: [{ name: "Membership snapshot record", role: "Voting member", nominator: "Political party" }],
    accounting: { registeredVoters: first.registeredVoters, ballotsIssued: first.ballotsIssued, validBallots: Math.max(0, first.ballotsIssued - 7), invalidBallots: 7 },
    partyResults: points.map((point) => ({ partyId: point.partyId, partyName: point.partyName, votes: point.partyVotes, share: point.partyShare })),
    candidateResults: [],
    protocols: [],
    specialFlags: first.specialFlags, matchStatus: first.matchStatus,
    validationMessages: first.matchStatus === "matched" ? [] : ["Commission metadata match requires review."],
    sources: [{ label: "CEC election result", url: "https://www.vybory.izbirkom.ru/" }, { label: "Commission membership snapshot", url: "https://www.cikrf.ru/" }],
  };
}
