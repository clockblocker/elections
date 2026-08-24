export type HistoricalProtocolSource = Readonly<{
  requestedUrl: string;
  finalUrl: string | null;
  retrievedAt: string | null;
  archiveCaptureTimestamp: string | null;
  sha256: string;
  provenance: "live-official" | "wayback";
  encoding: string;
  extractionMethod: "plain-html-direct-protocol";
  derivation: "direct";
}>;

export type HistoricalTikProtocol = Readonly<{
  election: "2011-duma";
  electionVrn: string;
  protocol: "party";
  reportType: number;
  commission: Readonly<{
    level: "tik";
    name: string;
    tvd: string;
    region: string | null;
  }>;
  accounting: Readonly<Record<string, number>>;
  votes: Readonly<Record<string, number>>;
  validation: Readonly<{
    vote_sum: number;
    valid_ballots: number | null;
    vote_sum_matches_valid_ballots: boolean;
  }>;
  source: HistoricalProtocolSource;
}>;
