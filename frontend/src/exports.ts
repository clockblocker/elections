import type { AnalyticalState, Point } from "./types";

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

const escapeCsv = (value: unknown) => {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
};

const safeToken = (value: string | null) => (value || "all").replace(/[^a-z0-9-]+/gi, "-").replace(/^-|-$/g, "").slice(0, 48) || "all";

function exportStem(filters: AnalyticalState, sourceVersion: string): string {
  return ["elections", filters.ballotKind, `district-${safeToken(filters.districtId)}`, `candidate-${safeToken(filters.candidateId)}`, `source-${safeToken(sourceVersion)}`].join("-");
}

export function pointsToCsv(points: Point[], sourceVersion: string, filters: AnalyticalState): string {
  const metadata = `# source_version=${sourceVersion}; filters=${encodeURIComponent(JSON.stringify(filters))}`;
  const keys: Array<keyof Point> = ["ballotKind", "ballotId", "districtId", "candidateId", "affiliation", "winner", "uikNumber", "tikName", "regionName", "partyName", "registeredVoters", "ballotsIssued", "turnout", "partyVotes", "partyShare", "matchStatus", "validationStatus"];
  return [metadata, keys.join(","), ...points.map((point) => keys.map((key) => escapeCsv(point[key])).join(","))].join("\n");
}

export function exportCsv(points: Point[], sourceVersion: string, filters: AnalyticalState): void {
  download(new Blob([pointsToCsv(points, sourceVersion, filters)], { type: "text/csv;charset=utf-8" }), `${exportStem(filters, sourceVersion)}-visible-points.csv`);
}

export function analysisToJson(filters: AnalyticalState, sourceVersion: string, exportedAt = new Date().toISOString()): string {
  return JSON.stringify({ schemaVersion: 2, sourceVersion, exportedAt, ballotKind: filters.ballotKind, districtId: filters.districtId, candidateId: filters.candidateId, formulas: { turnout: "(portable_box_ballots + stationary_box_ballots) / registered_voters * 100", result: `${filters.ballotKind === "single_member" ? "candidate" : "party"}_votes / valid_ballots * 100` }, filters, selection: filters.selectedId }, null, 2);
}

export function exportJson(filters: AnalyticalState, sourceVersion: string): void {
  download(new Blob([analysisToJson(filters, sourceVersion)], { type: "application/json" }), `${exportStem(filters, sourceVersion)}-analysis.json`);
}

export function pngFilename(filters: AnalyticalState, sourceVersion: string): string {
  return `${exportStem(filters, sourceVersion)}-scatterplot.png`;
}

export function exportPng(dataUrl: string, filters: AnalyticalState, sourceVersion: string): void {
  const anchor = document.createElement("a");
  anchor.href = dataUrl;
  anchor.download = pngFilename(filters, sourceVersion);
  anchor.click();
}
