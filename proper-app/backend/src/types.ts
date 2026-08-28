export type VoteMap = Readonly<Record<string, number>>;

export interface ProtocolSource {
  url: string;
  sha256: string;
  sourceReportType: 233 | 242;
  derivation?: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}

export interface PartyProtocol {
  election: "2021-duma";
  level: "uik";
  reportType: 242;
  ballot: "party";
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}

export interface CoverageRegion {
  region: string;
  discovered_tik_count: number;
  discovered_uik_count: number;
  uiks_with_party_results: number;
  missing_uiks: unknown[];
}

export interface CoverageDocument {
  schema_version: number;
  election: "2021-duma";
  regions: CoverageRegion[];
}

export interface ScatterPoint {
  id: string;
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionName: string;
  registeredVoters: number;
  validBallots: number;
  ballotsCounted: number;
  optionVotes: number;
  turnout: number | null;
  result: number | null;
}
