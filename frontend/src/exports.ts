import type { AnalyticalState, Point } from "./types";

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

const escapeCsv = (value: string | number) => {
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
};

export function pointsToCsv(points: Point[], sourceVersion: string, filters: AnalyticalState): string {
  const metadata = `# source_version=${sourceVersion}; filters=${encodeURIComponent(JSON.stringify(filters))}`;
  const keys: Array<keyof Point> = ["uikNumber", "tikName", "regionName", "partyName", "registeredVoters", "ballotsIssued", "turnout", "partyVotes", "partyShare", "matchStatus"];
  return [metadata, keys.join(","), ...points.map((point) => keys.map((key) => escapeCsv(point[key] as string | number)).join(","))].join("\n");
}

export function exportCsv(points: Point[], sourceVersion: string, filters: AnalyticalState): void {
  download(new Blob([pointsToCsv(points, sourceVersion, filters)], { type: "text/csv;charset=utf-8" }), "elections-visible-points.csv");
}

export function exportJson(filters: AnalyticalState, sourceVersion: string): void {
  download(new Blob([JSON.stringify({ schemaVersion: 1, sourceVersion, exportedAt: new Date().toISOString(), formulas: { turnout: "ballots_issued / registered_voters * 100", result: "party_votes / valid_party_votes * 100" }, filters, selection: filters.selectedId }, null, 2)], { type: "application/json" }), "elections-analysis.json");
}

export function exportPng(dataUrl: string): void {
  const anchor = document.createElement("a");
  anchor.href = dataUrl;
  anchor.download = "elections-scatterplot.png";
  anchor.click();
}
