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
  turnout: number | null;
  result: number | null;
}

export interface AnalysisParameters {
  coreFraction: number;
  coreIterations: number;
  covarianceRidge: number;
  fdrThreshold: number;
  reviewThreshold: number;
}

export interface PointEstimate {
  status: "scored" | "unscored";
  reason?: "no-valid-ballots" | "no-registered-voters" | "invalid-accounting" | "clt-small-expected-count" | "no-peer-model";
  grade: "P0" | "P1" | "P2" | "P3" | "U";
  observedVotes: number;
  observedShare: number | null;
  baselineSource: string;
  peerPrecincts: number;
  peerBallots: number;
  expectedTurnout: number | null;
  expectedShare: number | null;
  expectedVotes: number | null;
  residualVotes: number | null;
  standardErrorVotes: number | null;
  interval95: [number, number] | null;
  zScore: number | null;
  pValue: number | null;
  qValue: number | null;
  pSus: number | null;
  overdispersion: number | null;
  mahalanobisSquared: number | null;
  direction: "high-high" | "high-low" | "low-high" | "low-low" | "central" | null;
  qualityFlags: string[];
}

export interface AnalysisResult {
  method: { slug: "protocol-cloud-clt-v3"; version: 3 };
  parameters: AnalysisParameters;
  protocols: number;
  scoredProtocols: number;
  unscoredProtocols: number;
  flaggedProtocols: number;
  observedVotes: number;
  expectedVotes: number;
  flaggedResidualVotes: number;
  core: {
    protocols: number;
    expectedTurnout: number;
    expectedResult: number;
    covariance: [[number, number], [number, number]];
    contour50: Array<{ turnout: number; result: number }>;
    contour95: Array<{ turnout: number; result: number }>;
  };
  estimates: Map<string, PointEstimate>;
}

export interface AnalysisSummary {
  protocols: number;
  scoredProtocols: number;
  unscoredProtocols: number;
  flaggedProtocols: number;
  observedVotes: number;
  expectedVotes: number;
  flaggedResidualVotes: number;
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
