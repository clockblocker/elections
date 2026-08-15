export type MatchStatus = "matched" | "partial" | "unmatched";

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
  registeredVoters: number;
  ballotsIssued: number;
  turnout: number;
  partyVotes: number;
  partyShare: number;
  specialFlags: string[];
  matchStatus: MatchStatus;
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
  specialFlags: string[];
  matchStatus: MatchStatus;
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
