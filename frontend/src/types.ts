export const MATCH_STATUSES = [
  "matched",
  "ambiguous",
  "result_only",
  "pending",
  "special",
  "commission_only",
  "data_integrity_error",
  // Kept for imported/demo API payloads created before exact backend statuses were exposed.
  "partial",
  "unmatched",
] as const;
export type MatchStatus = (typeof MATCH_STATUSES)[number];
export type BallotKind = "party_list" | "single_member";

export interface Party {
  id: string;
  name: string;
  shortName: string;
  color: string;
}

export interface FilterOption {
  id: string;
  name: string;
  count?: number;
}

export interface FilterMetadata {
  parties: Party[];
  ballots: Array<{ id: string; kind: BallotKind; name: string; scopeKey: string; districtId?: string }>;
  districts: Array<FilterOption & { ballotId: string; code: string; regionName?: string }>;
  candidates: Array<FilterOption & { ballotId: string; districtId: string; affiliation?: string; winner: boolean; position: number }>;
  affiliations: FilterOption[];
  regions: FilterOption[];
  tiks: FilterOption[];
  specialTypes: FilterOption[];
  matchStatuses: MatchStatus[];
  sourceVersion: string;
  generatedAt?: string;
}

export interface Point {
  id: string;
  uikId: string;
  uikNumber: string;
  tikId: string;
  tikName: string;
  regionId: string;
  regionName: string;
  partyId: string;
  partyName: string;
  ballotId: string;
  ballotKind: BallotKind;
  districtId: string | null;
  candidateId: string | null;
  affiliation: string | null;
  winner: boolean;
  registeredVoters: number;
  ballotsIssued: number;
  turnout: number;
  partyVotes: number;
  partyShare: number;
  specialFlags: string[];
  matchStatus: MatchStatus;
  matchingMethod?: string | null;
  validationStatus?: string | null;
}

export interface Person {
  name: string;
  role?: string;
  nominator?: string | null;
}

export interface SourceLink {
  label: string;
  url: string;
  checksum?: string;
}

export interface PrecinctDetail {
  id: string;
  uikNumber: string;
  tikName: string;
  regionName: string;
  districtName?: string | null;
  address?: string | null;
  chairperson?: Person | null;
  members?: Person[] | null;
  accounting: Record<string, number | null>;
  partyResults: Array<{ partyId: string; partyName: string; votes: number; share: number }>;
  candidateResults: Array<{ candidateId: string; candidateName: string; affiliation?: string | null; votes: number; share: number; winner: boolean }>;
  protocols: Array<{
    resultRecordId: string;
    ballotKind: BallotKind;
    ballotName: string;
    turnout: number | null;
    partyResults: Array<{ partyId: string; partyName: string; votes: number; share: number }>;
    candidateResults: Array<{ candidateId: string; candidateName: string; affiliation?: string | null; votes: number; share: number; winner: boolean }>;
    sources: SourceLink[];
  }>;
  specialFlags: string[];
  matchStatus: MatchStatus;
  matchingMethod?: string | null;
  gasVyboryId?: string | null;
  gasResolutionStatus?: string | null;
  gasResolutionReason?: string | null;
  validationMessages?: string[];
  sources: SourceLink[];
}

export interface DatasetStatus {
  ready: boolean;
  sourceVersion: string;
  reconciled: boolean;
  pointCount: number;
  updatedAt?: string;
}

export interface AnalyticalState {
  ballotKind: BallotKind;
  ballotId: string | null;
  districtId: string | null;
  candidateId: string | null;
  affiliations: string[];
  winner: boolean | null;
  partyIds: string[];
  regionIds: string[];
  tikIds: string[];
  specialTypes: string[];
  matchStatuses: MatchStatus[];
  turnoutMin: number;
  turnoutMax: number;
  resultMin: number;
  resultMax: number;
  selectedId: string | null;
}

export interface CosmeticPreferences {
  pointSize: number;
  highContrast: boolean;
  showGrid: boolean;
}
