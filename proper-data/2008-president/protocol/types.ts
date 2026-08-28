// Generated protocol files use these structural contracts. No database is involved.

export type VoteMap = Readonly<Record<string, number>>;

export type DistrictRef = Readonly<{
  districtNumber: number;
  oikTvd: string;
  oikName: string;
}>;

export type ProtocolSource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 227 | 226;
  derivation: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

type UikProtocolBase = Readonly<{
  election: "2008-president";
  level: "uik";
  uikNumber: number;
  uikTvd: string;
  uikName: string;
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}>;

export type PresidentialUikProtocol = Readonly<UikProtocolBase & {
  reportType: 226;
  ballot: "presidential";
}>;

export type UikProtocol = PresidentialUikProtocol;

type TicProtocolBase = Readonly<{
  election: "2008-president";
  level: "tic";
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  uikCount: number;
  accounting: VoteMap;
  votes: VoteMap;
  uikTvds: readonly string[];
  source: ProtocolSource;
}>;

export type PresidentialTicProtocol = Readonly<TicProtocolBase & {
  reportType: 227;
  ballot: "presidential";
}>;

export type TicProtocol = PresidentialTicProtocol;

export type UikTikRelation = Readonly<{
  uikNumber: number;
  uikTvd: string;
  uikName: string;
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  district: DistrictRef | null;
}>;
