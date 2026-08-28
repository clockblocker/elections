export interface PartyOption {
  id: number;
  position: number;
  name: string;
  shortName: string;
  color: string;
  votes: string;
}

export interface Region {
  code: string;
  name: string;
  precincts: number;
}

export interface Metadata {
  election: { slug: string; name: string; electionDate: string; scopeNote: string };
  coverage: {
    regions: number; tiks: number; discoveredUiks: number; importedProtocols: number;
    missingProtocols: number; degPolicy: string; updatedAt: string;
  };
  options: PartyOption[];
  regions: Region[];
  method: { slug: string; version: number; name: string; description: string; parameters: AnalysisParameters };
}

export interface Point {
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
  turnout: number;
  result: number;
}

export interface AnalysisParameters {
  referenceTurnoutMin: number;
  referenceTurnoutMax: number;
  analysisTurnoutMin: number;
  positiveExcessOnly: boolean;
}

export interface PointEstimate {
  baselineSource: string;
  baselineOdds: number;
  expectedVotes: number;
  excessVotes: number;
}

export interface AnalysisResult {
  points: number;
  referencePoints: number;
  analyzedPoints: number;
  regionsWithLocalBaseline: number;
  baselineOdds: number;
  baselineShare: number;
  observedVotes: number;
  expectedVotes: number;
  estimatedExcessVotes: number;
  estimates: Map<string, PointEstimate>;
}

export interface ProtocolDetail {
  id: string;
  precinct: {
    uikNumber: number; uikTvd: string; tikTvd: string; tikName: string;
    regionCode: string; regionName: string; kind: "physical";
  };
  accounting: Record<string, number>;
  votes: Array<{ position: number; name: string; shortName: string; color: string; votes: number; result: number | null }>;
  source: {
    url: string; finalUrl?: string; sha256: string; provenance: string;
    sourceReportType: number; derivation?: string; retrievedAt?: string;
  };
}
