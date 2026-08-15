import { describe, expect, it } from "vitest";
import { demoPoints } from "./demo";
import { analysisToJson, pngFilename, pointsToCsv } from "./exports";
import { DEFAULT_STATE } from "./state";

describe("CSV export", () => {
  it("describes source version and filters before visible rows", () => {
    const point = demoPoints(DEFAULT_STATE)[0];
    const csv = pointsToCsv([point], "2021-r3", DEFAULT_STATE);
    expect(csv).toContain("# source_version=2021-r3");
    expect(csv).toContain("ballotKind,ballotId,districtId,candidateId,affiliation,winner");
    expect(csv).toContain(point.regionName);
  });

  it("identifies the analytical scope in PNG filenames", () => {
    expect(pngFilename({ ...DEFAULT_STATE, ballotKind: "single_member", districtId: "oik-42", candidateId: "candidate-7" }, "2021-r3"))
      .toBe("elections-single_member-district-oik-42-candidate-candidate-7-source-2021-r3-scatterplot.png");
  });

  it("exports formulas, provenance, district, candidate, filters, and selection", () => {
    const state = { ...DEFAULT_STATE, ballotKind: "single_member" as const, districtId: "oik-42", candidateId: "candidate-7", selectedId: "result-9" };
    const document = JSON.parse(analysisToJson(state, "2021-r3", "2026-08-15T00:00:00Z"));
    expect(document).toEqual(expect.objectContaining({ schemaVersion: 2, sourceVersion: "2021-r3", ballotKind: "single_member", districtId: "oik-42", candidateId: "candidate-7", selection: "result-9" }));
    expect(document.formulas.result).toContain("candidate_votes");
    expect(document.filters).toEqual(state);
  });
});
