// Generated protocol files use these structural contracts. No database is involved.

export type VoteMap = Readonly<Record<string, number>>;

export type ProtocolSource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 227 | 226;
  derivation: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

export type UikProtocol = Readonly<{
  election: "2018-president";
  level: "uik";
  reportType: 226;
  ballot: "presidential";
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}>;

export type TicProtocol = Readonly<{
  election: "2018-president";
  level: "tic";
  reportType: 227;
  ballot: "presidential";
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
