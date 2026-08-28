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
  sourceReportType: 226 | 227;
  derivation: "direct" | "extracted-tic-column";
  retrievedAt?: string;
  finalUrl?: string;
  provenance?: "live-official" | "wayback";
}>;

export type WinnerRegistrySource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType?: number;
  retrievedAt: string;
  finalUrl: string;
  provenance: "live-official" | "wayback";
  resolution?: string;
  resolutionDate?: string;
  title?: string;
  winnerDerivation?: "summed-official-tik-results-validated-against-oik-total";
  oikUniqueHighestVoteTotal?: number;
  summedTikWinnerVoteTotal?: number;
}>;

export type DistrictWinnerRegistrySource = Readonly<WinnerRegistrySource & {
  winnerDerivation: "summed-official-tik-results-validated-against-oik-total";
  oikUniqueHighestVoteTotal: number;
  summedTikWinnerVoteTotal: number;
}>;

export type CandidateRegistrySource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 220 | 221;
  retrievedAt: string;
  finalUrl: string;
  provenance: "live-official" | "wayback";
  winnerSource: WinnerRegistrySource;
}>;

export type DistrictCandidateRegistrySource = Readonly<
  Omit<CandidateRegistrySource, "winnerSource"> & {
    winnerSource: DistrictWinnerRegistrySource;
  }
>;

export type PartyRegistrySource = Readonly<{
  url: string;
  sha256: string;
  sourceReportType: 236 | 303;
  retrievedAt: string;
  finalUrl: string;
  provenance: "live-official" | "wayback";
}>;

type RegistryCandidate = Readonly<{
  voteKey: `gas:candidate-vibid:${string}`;
  candidateVibid: string;
  fullName: string;
  nominatingEntity: string;
  registrationStatus: string;
  isElected: boolean;
  /** Literal election-status flag in the candidate registry when it differs. */
  registryIsElected?: boolean;
}>;

export type DistrictCandidate = RegistryCandidate;
export type PresidentialCandidate = RegistryCandidate;

export type DistrictResultIdentityAnomaly = Readonly<{
  districtNumber: number;
  voteKey: `special:official-result-label:${number}`;
  rawLabel: string;
  reason: string;
  observedLevels: readonly ("oik" | "tik" | "uik")[];
  observedCounts: Readonly<{ oik: number; tik: number; uik: number }>;
}>;

export type ObfuscatedOikWinnerLabelAnomaly = Readonly<{
  districtNumber: number;
  rawLabel: string;
  winnerCandidateKey: `gas:candidate-vibid:${string}`;
  winnerCandidateVibid: string;
}>;

type SingleMemberDistrictBase = Readonly<{
  election: "2012-president";
  districtNumber: number;
  oikTvd: string;
  oikName: string;
  regionCode: string;
  regionTvd: string;
  regionName: string;
  candidates: readonly DistrictCandidate[];
  resultIdentityAnomalies: readonly DistrictResultIdentityAnomaly[];
  obfuscatedWinnerLabelAnomalies: readonly ObfuscatedOikWinnerLabelAnomaly[];
  source: DistrictCandidateRegistrySource;
}>;

export type CandidateWinnerDistrict = Readonly<SingleMemberDistrictBase & {
  winnerCandidateVibid: string;
  specialWinnerKey?: never;
}>;

export type SpecialWinnerDistrict = Readonly<SingleMemberDistrictBase & {
  winnerCandidateVibid: null;
  specialWinnerKey: string;
}>;

export type SingleMemberDistrict = CandidateWinnerDistrict | SpecialWinnerDistrict;

export type PresidentialCandidateCatalog = Readonly<{
  election: "2012-president";
  winnerCandidateVibid: string;
  candidates: readonly PresidentialCandidate[];
  source: CandidateRegistrySource;
}>;

export type PartyRegistryDecision = Readonly<{
  date: string;
  number: string;
}>;

type DumaPartyChoiceBase = Readonly<{
  voteKey: string;
  officialName: string;
  isOnFederalBallot: boolean;
  registrationStatus?: string;
  catalogOrdinal?: number;
  ballotNumber?: number;
  entityKind?: string;
  charterRegistrationDate?: string;
  justiceRegistryNumber?: string;
  federalListCertification?: PartyRegistryDecision;
  federalListRegistration?: PartyRegistryDecision;
  singleMemberListCertification?: PartyRegistryDecision;
  listMandates?: number;
  officialVotes?: number;
  officialVotePercent?: string;
}>;

export type ModernDumaPartyChoice = Readonly<DumaPartyChoiceBase & {
  voteKey: `gas:vrnio:${string}`;
  electoralAssociationVrnio: string;
}>;

export type LegacyDumaPartyChoice = Readonly<DumaPartyChoiceBase & {
  voteKey: `gas:association-vibid:${string}`;
  associationVibid: string;
  listVibid: string;
  detailSource: PartyRegistrySource;
}>;

export type SpecialDumaPartyChoice = Readonly<DumaPartyChoiceBase & {
  voteKey: `special:${string}`;
}>;

export type DumaPartyChoice =
  | ModernDumaPartyChoice
  | LegacyDumaPartyChoice
  | SpecialDumaPartyChoice;

export type DumaPartyCatalog = Readonly<{
  election: "2012-president";
  choices: readonly DumaPartyChoice[];
  source: PartyRegistrySource;
  detailSources?: readonly PartyRegistrySource[];
}>;

type UikProtocolBase = Readonly<{
  election: "2012-president";
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
  election: "2012-president";
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
