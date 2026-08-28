export type VoteMap = Readonly<Record<string, number>>;

export interface ProtocolSource {
  url: string;
  sha256: string;
  sourceReportType: number;
  derivation?: string;
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}

export interface ElectionProtocol {
  election: string;
  level: "uik";
  reportType: number;
  ballot: "party" | "presidential";
  uikNumber: number;
  uikTvd: string;
  uikName?: string;
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionTvd?: string;
  regionName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}

export interface CoverageRegion {
  region: string;
  regionCode?: string;
  regionTvd?: string;
  regionName?: string;
  discovered_tik_count: number;
  discovered_uik_count: number;
  uiks_with_party_results?: number;
  uiks_with_candidate_results?: number;
  missing_uiks?: unknown[];
}

export interface CoverageDocument {
  schema_version: number;
  election: string;
  generated_at?: string;
  regions: CoverageRegion[];
}

export interface CatalogChoice {
  voteKey: string;
  officialName?: string;
  fullName?: string;
  ballotNumber?: number;
}

export interface BallotCatalog {
  choices?: CatalogChoice[];
  candidates?: CatalogChoice[];
}

export interface ScatterPoint {
  id: string;
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  regionKey: string;
  regionCode: string;
  regionName: string;
  registeredVoters: number;
  validBallots: number;
  ballotsCounted: number;
  optionVotes: number;
  turnout: number | null;
  result: number | null;
}
