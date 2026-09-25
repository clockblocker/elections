export type ElectionSlug =
  | "2003-duma"
  | "2004-president"
  | "2007-duma"
  | "2008-president"
  | "2011-duma"
  | "2012-president"
  | "2013-moscow-mayor"
  | "2016-duma"
  | "2018-president"
  | "2021-duma"
  | "2024-president";

export interface ElectionConfig {
  slug: ElectionSlug;
  name: string;
  electionDate: string;
  year: number;
  electionKind: "duma" | "president" | "mayor";
  ballotSourceKind: "party" | "presidential";
  ballotKind: "party-list" | "presidential" | "mayoral";
  ballotName: string;
  reportType: 226 | 234 | 242 | 430;
  coverageResultKey: "uiks_with_party_results" | "uiks_with_candidate_results";
  catalogFile: "parties.ts" | "candidates.ts" | null;
  zeroWhenMissingAccounting?: readonly "ballots_issued_early"[];
  scopeNote: string;
}

export const ELECTIONS: readonly ElectionConfig[] = [
  {
    slug: "2003-duma", name: "State Duma election, fourth convocation", electionDate: "2003-12-07",
    year: 2003, electionKind: "duma", ballotSourceKind: "party", ballotKind: "party-list",
    ballotName: "Federal party list", reportType: 430, coverageResultKey: "uiks_with_party_results",
    catalogFile: "parties.ts",
    scopeNote: "Federal party-list results at physical UIKs. Single-member districts are outside this nationwide option model."
  },
  {
    slug: "2004-president", name: "Presidential election", electionDate: "2004-03-14",
    year: 2004, electionKind: "president", ballotSourceKind: "presidential", ballotKind: "presidential",
    ballotName: "Presidential ballot", reportType: 226, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts", scopeNote: "Presidential results at physical UIKs."
  },
  {
    slug: "2007-duma", name: "State Duma election, fifth convocation", electionDate: "2007-12-02",
    year: 2007, electionKind: "duma", ballotSourceKind: "party", ballotKind: "party-list",
    ballotName: "Federal party list", reportType: 242, coverageResultKey: "uiks_with_party_results",
    catalogFile: "parties.ts", scopeNote: "Federal party-list results at physical UIKs."
  },
  {
    slug: "2008-president", name: "Presidential election", electionDate: "2008-03-02",
    year: 2008, electionKind: "president", ballotSourceKind: "presidential", ballotKind: "presidential",
    ballotName: "Presidential ballot", reportType: 226, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts", scopeNote: "Presidential results at physical UIKs."
  },
  {
    slug: "2011-duma", name: "State Duma election, sixth convocation", electionDate: "2011-12-04",
    year: 2011, electionKind: "duma", ballotSourceKind: "party", ballotKind: "party-list",
    ballotName: "Federal party list", reportType: 242, coverageResultKey: "uiks_with_party_results",
    catalogFile: "parties.ts", scopeNote: "Federal party-list results at physical UIKs."
  },
  {
    slug: "2012-president", name: "Presidential election", electionDate: "2012-03-04",
    year: 2012, electionKind: "president", ballotSourceKind: "presidential", ballotKind: "presidential",
    ballotName: "Presidential ballot", reportType: 226, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts", scopeNote: "Presidential results at physical UIKs."
  },
  {
    slug: "2013-moscow-mayor", name: "Moscow mayoral election", electionDate: "2013-09-08",
    year: 2013, electionKind: "mayor", ballotSourceKind: "presidential", ballotKind: "mayoral",
    ballotName: "Mayoral ballot", reportType: 234, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts",
    zeroWhenMissingAccounting: ["ballots_issued_early"],
    scopeNote: "Moscow mayoral results at physical UIKs."
  },
  {
    slug: "2016-duma", name: "State Duma election, seventh convocation", electionDate: "2016-09-18",
    year: 2016, electionKind: "duma", ballotSourceKind: "party", ballotKind: "party-list",
    ballotName: "Federal party list", reportType: 242, coverageResultKey: "uiks_with_party_results",
    catalogFile: "parties.ts",
    scopeNote: "Federal party-list results at physical UIKs. Single-member districts are outside this nationwide option model."
  },
  {
    slug: "2018-president", name: "Presidential election", electionDate: "2018-03-18",
    year: 2018, electionKind: "president", ballotSourceKind: "presidential", ballotKind: "presidential",
    ballotName: "Presidential ballot", reportType: 226, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts", scopeNote: "Presidential results at physical UIKs."
  },
  {
    slug: "2021-duma", name: "State Duma election, eighth convocation", electionDate: "2021-09-19",
    year: 2021, electionKind: "duma", ballotSourceKind: "party", ballotKind: "party-list",
    ballotName: "Federal party list", reportType: 242, coverageResultKey: "uiks_with_party_results",
    catalogFile: null,
    scopeNote: "Federal party-list results at physical UIKs. DEG and single-member districts are outside this nationwide option model."
  },
  {
    slug: "2024-president", name: "Presidential election", electionDate: "2024-03-17",
    year: 2024, electionKind: "president", ballotSourceKind: "presidential", ballotKind: "presidential",
    ballotName: "Presidential ballot", reportType: 226, coverageResultKey: "uiks_with_candidate_results",
    catalogFile: "candidates.ts", scopeNote: "Presidential results at physical UIKs. DEG is outside this analytical dataset."
  }
] as const;

export function electionConfig(slug: string): ElectionConfig {
  const config = ELECTIONS.find((item) => item.slug === slug);
  if (!config) throw new Error(`Unsupported election: ${slug}`);
  return config;
}

export const OPTION_COLORS = [
  "#315f9d", "#d54b3d", "#4d8766", "#d09a30", "#8b6c9d", "#27a6a1",
  "#c78554", "#6ca84f", "#686f79", "#982f36", "#70a897", "#9b8050",
  "#437fc7", "#9a6a7a", "#5d6b8d", "#b2694f", "#477f7a", "#8a6f43",
  "#715b88", "#3f718c", "#9a5b64", "#568155", "#786d67", "#5f6580"
] as const;

export const PARTY_STYLE_2021: Readonly<Record<number, { shortName: string; color: string }>> = {
  1: { shortName: "КПРФ", color: "#d54b3d" },
  2: { shortName: "Зелёные", color: "#4d8766" },
  3: { shortName: "ЛДПР", color: "#437fc7" },
  4: { shortName: "Новые люди", color: "#27a6a1" },
  5: { shortName: "Единая Россия", color: "#315f9d" },
  6: { shortName: "СРЗП", color: "#d09a30" },
  7: { shortName: "Яблоко", color: "#6ca84f" },
  8: { shortName: "Партия Роста", color: "#c78554" },
  9: { shortName: "РПСС", color: "#8b6c9d" },
  10: { shortName: "Коммунисты России", color: "#982f36" },
  11: { shortName: "Гражданская Платформа", color: "#686f79" },
  12: { shortName: "Зелёная Альтернатива", color: "#70a897" },
  13: { shortName: "Родина", color: "#9b8050" },
  14: { shortName: "Партия пенсионеров", color: "#9a6a7a" }
};
