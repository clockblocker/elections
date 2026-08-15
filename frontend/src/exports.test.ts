import { describe, expect, it } from "vitest";
import { demoPoints } from "./demo";
import { pointsToCsv } from "./exports";
import { DEFAULT_STATE } from "./state";

describe("CSV export", () => {
  it("describes source version and filters before visible rows", () => {
    const point = demoPoints(DEFAULT_STATE)[0];
    const csv = pointsToCsv([point], "2021-r3", DEFAULT_STATE);
    expect(csv).toContain("# source_version=2021-r3");
    expect(csv).toContain("uikNumber,tikName,regionName");
    expect(csv).toContain(point.regionName);
  });
});
