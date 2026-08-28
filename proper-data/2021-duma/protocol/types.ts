// Generated protocol files use these structural contracts. No database is involved.

export type VoteMap = Readonly<Record<string, number>>;

export type ProtocolSource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 233 | 242 | 463 | 464;
  derivation?: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

export type CandidateRegistrySource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 220;
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

export type DistrictCandidate = Readonly<{
  candidateVibid: string;
  fullName: string;
  nominatingEntity: string;
  registrationStatus: string;
  isElected: boolean;
}>;

export type DistrictRef = Readonly<{
  districtNumber: number;
  oikTvd: string;
}>;

export type SingleMemberDistrict = Readonly<{
  election: "2021-duma";
  districtNumber: number;
  oikTvd: string;
  oikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  winnerCandidateVibid: string;
  candidates: readonly DistrictCandidate[];
  source: CandidateRegistrySource;
}>;

type UikProtocolBase = Readonly<{
  election: "2021-duma";
  level: "uik";
  uikNumber: number;
  uikTvd: string;
  tikTvd: string;
  tikName: string;
  accounting: VoteMap;
  votes: VoteMap;
  source: ProtocolSource;
}>;

export type UikPartyProtocol = UikProtocolBase & Readonly<{
  reportType: 242;
  ballot: "party";
}>;

export type UikSingleMemberProtocol = UikProtocolBase & Readonly<{
  reportType: 463;
  ballot: "single-member";
  district: DistrictRef;
}>;

export type UikProtocol = UikPartyProtocol | UikSingleMemberProtocol;

type TicProtocolBase = Readonly<{
  election: "2021-duma";
  level: "tic";
  tikTvd: string;
  tikName: string;
  uikCount: number;
  accounting: VoteMap;
  votes: VoteMap;
  uikTvds: readonly string[];
  source: ProtocolSource;
}>;

export type TicPartyProtocol = TicProtocolBase & Readonly<{
  reportType: 233;
  ballot: "party";
}>;

export type TicSingleMemberProtocol = TicProtocolBase & Readonly<{
  reportType: 464;
  ballot: "single-member";
  district: DistrictRef;
}>;

export type TicProtocol = TicPartyProtocol | TicSingleMemberProtocol;

export type UikTikRelation = Readonly<{
  uikNumber: number;
  uikTvd: string;
  uikName: string;
  tikTvd: string;
  tikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  district: DistrictRef;
  oikTvd: string;
  oikName: string;
}>;
