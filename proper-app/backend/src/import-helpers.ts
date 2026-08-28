import { ACCOUNTING_KEYS, PARTY_STYLE } from "./constants";
import type { PartyProtocol } from "./types";

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

export function accountingValues(protocol: PartyProtocol): number[] {
  const value = (key: string) => {
    const result = protocol.accounting[key];
    if (!Number.isInteger(result) || result < 0) throw new Error(`Invalid accounting value ${key} for UIK ${protocol.uikTvd}`);
    return result;
  };
  return [
    value(ACCOUNTING_KEYS.registeredVoters), value(ACCOUNTING_KEYS.ballotsReceived),
    value(ACCOUNTING_KEYS.ballotsIssuedEarly), value(ACCOUNTING_KEYS.ballotsIssuedAtStation),
    value(ACCOUNTING_KEYS.ballotsIssuedOutside), value(ACCOUNTING_KEYS.ballotsCancelled),
    value(ACCOUNTING_KEYS.portableBoxBallots), value(ACCOUNTING_KEYS.stationaryBoxBallots),
    value(ACCOUNTING_KEYS.invalidBallots), value(ACCOUNTING_KEYS.validBallots),
    value(ACCOUNTING_KEYS.lostBallots), value(ACCOUNTING_KEYS.unaccountedBallots)
  ];
}

export function parseOption(label: string): { position: number; name: string; shortName: string; color: string } {
  const match = /^(\d+)\.\s*(.+)$/.exec(label);
  if (!match) throw new Error(`Ballot option has no numeric position: ${label}`);
  const position = Number(match[1]);
  const style = PARTY_STYLE[position];
  if (!style) throw new Error(`Unknown 2021 party-list position: ${position}`);
  return { position, name: match[2], ...style };
}

export function protocolArray(module: Record<string, unknown>, file: string): PartyProtocol[] {
  const value = Object.values(module).find(Array.isArray);
  if (!Array.isArray(value)) throw new Error(`No generated protocol array exported by ${file}`);
  for (const item of value) {
    const record = item as Partial<PartyProtocol>;
    if (record.election !== "2021-duma" || record.ballot !== "party" || record.reportType !== 242) {
      throw new Error(`Unexpected protocol in ${file}`);
    }
  }
  return value as PartyProtocol[];
}

export function placeholders(rowCount: number, columnCount: number, start = 1): string {
  let parameter = start;
  return Array.from({ length: rowCount }, () => `(${Array.from({ length: columnCount }, () => `$${parameter++}`).join(",")})`).join(",");
}

export function flatten(rows: readonly (readonly unknown[])[]): unknown[] {
  return rows.flatMap((row) => [...row]);
}
