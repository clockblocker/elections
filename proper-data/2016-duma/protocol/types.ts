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
  sourceReportType: 233 | 242 | 464 | 463;
  derivation: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

type UikProtocolBase = Readonly<{
  election: "2016-duma";
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

export type PartyUikProtocol = Readonly<UikProtocolBase & {
  reportType: 242;
  ballot: "party";
}>;
export type SingleMemberUikProtocol = Readonly<UikProtocolBase & {
  reportType: 463;
  ballot: "single-member";
  district: DistrictRef;
}>;

export type UikProtocol = PartyUikProtocol | SingleMemberUikProtocol;

type TicProtocolBase = Readonly<{
  election: "2016-duma";
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

export type PartyTicProtocol = Readonly<TicProtocolBase & {
  reportType: 233;
  ballot: "party";
}>;
export type SingleMemberTicProtocol = Readonly<TicProtocolBase & {
  reportType: 464;
  ballot: "single-member";
  district: DistrictRef;
}>;

export type TicProtocol = PartyTicProtocol | SingleMemberTicProtocol;

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
