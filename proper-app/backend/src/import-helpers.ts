import { OPTION_COLORS, PARTY_STYLE_2021, type ElectionConfig } from "./constants";
import type { BallotCatalog, ElectionProtocol } from "./types";

export const ACCOUNTING_COLUMNS = [
  "registered_voters",
  "ballots_received",
  "ballots_issued_early",
  "ballots_issued_at_station",
  "ballots_issued_outside",
  "ballots_cancelled",
  "portable_box_ballots",
  "stationary_box_ballots",
  "invalid_ballots",
  "valid_ballots",
  "lost_ballots",
  "unaccounted_ballots"
] as const;

const ACCOUNTING_MATCHERS: readonly ((label: string) => boolean)[] = [
  (label) => /избирател/.test(label) && /спис/.test(label) && /(внес|включ)/.test(label) && !/бюллет/.test(label),
  (label) => /бюллет/.test(label) && /полученн/.test(label) && !/не учтен/.test(label),
  (label) => /бюллет/.test(label) && /выданн/.test(label) && /досроч/.test(label),
  (label) => /бюллет/.test(label) && /выданн/.test(label) && !/(досроч|вне помещ|тик)/.test(label),
  (label) => /бюллет/.test(label) && /выданн/.test(label) && /вне помещ/.test(label),
  (label) => /бюллет/.test(label) && /погашенн/.test(label),
  (label) => /бюллет/.test(label) && /переносн/.test(label) && /ящик/.test(label),
  (label) => /бюллет/.test(label) && /стационарн/.test(label) && /ящик/.test(label),
  (label) => /бюллет/.test(label) && /недействительн/.test(label),
  (label) => /бюллет/.test(label) && /действительн/.test(label) && !/недействительн/.test(label),
  (label) => /бюллет/.test(label) && /утраченн/.test(label),
  (label) => /бюллет/.test(label) && /не учтен/.test(label)
];

export function accountingValues(protocol: ElectionProtocol, config?: ElectionConfig): number[] {
  const entries = Object.entries(protocol.accounting).map(([label, value]) => [label.toLocaleLowerCase("ru"), label, value] as const);
  return ACCOUNTING_MATCHERS.map((matches, index) => {
    const found = entries.filter(([normalized]) => matches(normalized));
    if (!found.length && config?.zeroWhenMissingAccounting?.includes(ACCOUNTING_COLUMNS[index] as "ballots_issued_early")) {
      return 0;
    }
    if (found.length !== 1) {
      throw new Error(`Expected one ${ACCOUNTING_COLUMNS[index]} field for UIK ${protocol.uikTvd}, found ${found.length}`);
    }
    const [, key, value] = found[0];
    if (!Number.isInteger(value) || value < 0) throw new Error(`Invalid accounting value ${key} for UIK ${protocol.uikTvd}`);
    return value;
  });
}

function cleanPartyName(name: string): string {
  return name
    .replace(/^[\s'"«]+|[\s'"»]+$/g, "")
    .replace(/^(всероссийская\s+)?политическая партия\s+/i, "")
    .replace(/^общественная организация\s+/i, "")
    .replace(/^партия\s+/i, "")
    .replace(/["«»]/g, "")
    .trim();
}

export function shortOptionName(name: string, kind: ElectionConfig["ballotKind"]): string {
  if (name.toLocaleUpperCase("ru").includes("ПРОТИВ ВСЕХ")) return "Против всех";
  if (kind === "presidential" || kind === "mayoral") return name.split(/\s+/)[0] || name;
  const normalized = name.toLocaleUpperCase("ru");
  if (normalized.includes("КОММУНИСТИЧЕСКАЯ ПАРТИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ")) return "КПРФ";
  if (normalized.includes("ЕДИНАЯ РОССИЯ")) return "Единая Россия";
  if (normalized.includes("ЛИБЕРАЛЬНО-ДЕМОКРАТИЧЕСК") || /\bЛДПР\b/.test(normalized)) return "ЛДПР";
  if (normalized.includes("СПРАВЕДЛИВАЯ РОССИЯ")) return "Справедливая Россия";
  if (normalized.includes("ЯБЛОКО")) return "Яблоко";
  const cleaned = cleanPartyName(name);
  return cleaned.length <= 34 ? cleaned : `${cleaned.slice(0, 31).trimEnd()}…`;
}

export function parseNumberedOption(label: string): { position: number; name: string; shortName: string; color: string } | null {
  const match = /^(\d+)\.\s*(.+)$/.exec(label);
  if (!match) return null;
  const position = Number(match[1]);
  const style = PARTY_STYLE_2021[position];
  return {
    position,
    name: match[2],
    shortName: style?.shortName ?? shortOptionName(match[2], "party-list"),
    color: style?.color ?? OPTION_COLORS[(position - 1) % OPTION_COLORS.length]
  };
}

export function ballotOptions(protocol: ElectionProtocol, config: ElectionConfig, catalog: BallotCatalog | null) {
  const choices = catalog?.choices ?? catalog?.candidates ?? [];
  const names = new Map(choices.map((choice) => [choice.voteKey, choice.officialName ?? choice.fullName ?? choice.voteKey]));
  return Object.keys(protocol.votes).map((key, index) => {
    const numbered = parseNumberedOption(key);
    if (numbered) return { key, ...numbered };
    const position = index + 1;
    const name = names.get(key) ?? (key === "special:against-all" ? "Против всех" : key);
    return {
      key, position, name, shortName: shortOptionName(name, config.ballotKind),
      color: OPTION_COLORS[index % OPTION_COLORS.length]
    };
  });
}

export function protocolArray(
  module: Record<string, unknown>,
  file: string,
  config: ElectionConfig
): ElectionProtocol[] {
  const value = Object.values(module).find(Array.isArray);
  if (!Array.isArray(value) || !value.length) throw new Error(`No generated protocol array exported by ${file}`);
  for (const item of value) {
    const record = item as Partial<ElectionProtocol>;
    if (record.election !== config.slug || record.level !== "uik" || record.ballot !== config.ballotSourceKind || record.reportType !== config.reportType) {
      throw new Error(`Unexpected protocol in ${file}`);
    }
  }
  return value as ElectionProtocol[];
}

export function placeholders(rowCount: number, columnCount: number, start = 1): string {
  let parameter = start;
  return Array.from({ length: rowCount }, () => `(${Array.from({ length: columnCount }, () => `$${parameter++}`).join(",")})`).join(",");
}

export function flatten(rows: readonly (readonly unknown[])[]): unknown[] {
  return rows.flatMap((row) => [...row]);
}
