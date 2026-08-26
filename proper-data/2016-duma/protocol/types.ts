// Generated protocol files use these structural contracts. No database is involved.

export type VoteMap = Readonly<Record<string, number>>;

export type ProtocolSource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 233 | 242 | 464 | 463;
  derivation: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

export type UikProtocol = Readonly<{
  election: "2016-duma";
  level: "uik";
  reportType: 242 | 463;
  ballot: "party" | "single-member";
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}>;

export type TicProtocol = Readonly<{
  election: "2016-duma";
  level: "tic";
  reportType: 233 | 464;
  ballot: "party" | "single-member";
  tikTvd: string;
  tikName: string;
  uikCount: number;
  accounting: VoteMap;
  votes: VoteMap;
  uikTvds: readonly string[];
  source: ProtocolSource;
}>;

export type UikTikRelation = Readonly<{
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
}>;
