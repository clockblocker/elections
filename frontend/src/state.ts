import type { AnalyticalState, CosmeticPreferences, MatchStatus } from "./types";

export const DEFAULT_STATE: AnalyticalState = {
  partyIds: ["united-russia"],
  regionIds: [],
  tikIds: [],
  specialTypes: [],
  matchStatuses: [],
  turnoutMin: 0,
  turnoutMax: 100,
  resultMin: 0,
  resultMax: 100,
  selectedId: null,
};

const array = (p: URLSearchParams, key: string) => p.getAll(key).filter(Boolean);
const boundedNumber = (value: string | null, fallback: number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : fallback;
};

export function parseAnalyticalState(search: string): AnalyticalState {
  const p = new URLSearchParams(search);
  return {
    partyIds: array(p, "party").length ? array(p, "party") : DEFAULT_STATE.partyIds,
    regionIds: array(p, "region"),
    tikIds: array(p, "tik"),
    specialTypes: array(p, "special"),
    matchStatuses: array(p, "match").filter((x): x is MatchStatus => ["matched", "partial", "unmatched"].includes(x)),
    turnoutMin: boundedNumber(p.get("turnoutMin"), 0),
    turnoutMax: boundedNumber(p.get("turnoutMax"), 100),
    resultMin: boundedNumber(p.get("resultMin"), 0),
    resultMax: boundedNumber(p.get("resultMax"), 100),
    selectedId: p.get("selected"),
  };
}

export function serializeAnalyticalState(state: AnalyticalState): string {
  const p = new URLSearchParams();
  state.partyIds.forEach((x) => p.append("party", x));
  state.regionIds.forEach((x) => p.append("region", x));
  state.tikIds.forEach((x) => p.append("tik", x));
  state.specialTypes.forEach((x) => p.append("special", x));
  state.matchStatuses.forEach((x) => p.append("match", x));
  if (state.turnoutMin !== 0) p.set("turnoutMin", String(state.turnoutMin));
  if (state.turnoutMax !== 100) p.set("turnoutMax", String(state.turnoutMax));
  if (state.resultMin !== 0) p.set("resultMin", String(state.resultMin));
  if (state.resultMax !== 100) p.set("resultMax", String(state.resultMax));
  if (state.selectedId) p.set("selected", state.selectedId);
  const result = p.toString();
  return result ? `?${result}` : "";
}

const PREFS_KEY = "elections-workbench:prefs:v1";
export const DEFAULT_PREFERENCES: CosmeticPreferences = { pointSize: 3, highContrast: false, showGrid: true };

export function readPreferences(): CosmeticPreferences {
  try {
    const stored = JSON.parse(localStorage.getItem(PREFS_KEY) || "null") as Partial<CosmeticPreferences> | null;
    return stored ? { ...DEFAULT_PREFERENCES, ...stored } : DEFAULT_PREFERENCES;
  } catch { return DEFAULT_PREFERENCES; }
}

export function savePreferences(value: CosmeticPreferences): void {
  localStorage.setItem(PREFS_KEY, JSON.stringify(value));
}
