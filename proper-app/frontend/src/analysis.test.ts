import { describe, expect, test } from "vitest";
import {
  DEFAULT_PARAMETERS, adjustBenjaminiHochberg, adjustBenjaminiYekutieli, analyze,
  gradeFor, summarizeAnalysis
} from "./analysis";
import type { Point } from "./types";

function point(
  id: string, optionVotes: number, validBallots = 1_000, turnout = 50,
  regionCode = "1", tikTvd = "tik-1"
): Point {
  return {
    id, regionCode, tikTvd, optionVotes, validBallots, turnout,
    registeredVoters: 2_000, ballotsCounted: Math.round(20 * turnout),
    result: validBallots > 0 ? 100 * optionVotes / validBallots : null,
    uikNumber: Number(id.replace(/\D/g, "")) || 1, uikTvd: id,
    tikName: tikTvd, regionName: regionCode
  };
}

function fixture(): Point[] {
  const points: Point[] = [];
  for (let index = 0; index < 80; index += 1) {
    const turnout = 35 + index % 20;
    const ordinary = 195 + (index % 7) * 2;
    points.push(point(`p${index}`, ordinary, 1_000, turnout, "1", `tik-${Math.floor(index / 20)}`));
  }
  return points;
}

describe("peer-clt-v2", () => {
  test("scores every eligible protocol and preserves explicit unscored reasons", () => {
    const points = [...fixture(), point("zero", 0, 0, 0)];
    const result = analyze(points, { ...DEFAULT_PARAMETERS, minRegionPeers: 10, minTikPeers: 5 });
    expect(result.protocols).toBe(points.length);
    expect(result.estimates.size).toBe(points.length);
    expect(result.estimates.get("zero")).toMatchObject({ status: "unscored", grade: "U", reason: "no-valid-ballots" });
    expect(result.scoredProtocols + result.unscoredProtocols).toBe(points.length);
  });

  test("uses n times the out-of-sample expected share and grades a large positive residual higher", () => {
    const points = fixture();
    points.push(point("moderate", 260, 1_000, 45, "1", "tik-1"));
    points.push(point("large", 500, 1_000, 45, "1", "tik-1"));
    const result = analyze(points, { ...DEFAULT_PARAMETERS, minRegionPeers: 10, minTikPeers: 5 });
    const moderate = result.estimates.get("moderate")!;
    const large = result.estimates.get("large")!;
    expect(moderate.expectedVotes).toBeCloseTo((moderate.expectedShare ?? 0) * 1_000);
    expect(large.pSus ?? 0).toBeGreaterThan(moderate.pSus ?? 0);
    expect(large.grade).toBe("P1");
  });

  test("adds empirical heterogeneity to the binomial CLT variance", () => {
    const points = fixture().map((item, index) => ({
      ...item,
      optionVotes: index % 2 ? 300 : 100,
      result: index % 2 ? 30 : 10
    }));
    const result = analyze(points, { ...DEFAULT_PARAMETERS, minRegionPeers: 10, minTikPeers: 5 });
    const estimate = result.estimates.get("p20")!;
    expect(estimate.overdispersion ?? 0).toBeGreaterThan(1);
    expect((estimate.interval95?.[1] ?? 0) - (estimate.interval95?.[0] ?? 0)).toBeGreaterThan(30);
  });

  test("uses known Benjamini-Hochberg adjusted values", () => {
    const adjusted = adjustBenjaminiHochberg([
      { id: "a", p: 0.01 }, { id: "b", p: 0.04 }, { id: "c", p: 0.03 }, { id: "d", p: 0.002 }
    ]);
    expect(adjusted.get("a")).toBeCloseTo(0.02);
    expect(adjusted.get("b")).toBeCloseTo(0.04);
    expect(adjusted.get("c")).toBeCloseTo(0.04);
    expect(adjusted.get("d")).toBeCloseTo(0.008);
    const by = adjustBenjaminiYekutieli([{ id: "a", p: 0.01 }, { id: "b", p: 0.04 }]);
    expect(by.get("a")).toBeCloseTo(0.03);
  });

  test("keeps P_sus severity separate from display summaries and FDR review flags", () => {
    expect(gradeFor(0.949)).toBe("P0");
    expect(gradeFor(0.95)).toBe("P1");
    expect(gradeFor(0.99)).toBe("P2");
    expect(gradeFor(0.999)).toBe("P3");
    expect(gradeFor(null)).toBe("U");
    const points = fixture();
    const result = analyze(points, { ...DEFAULT_PARAMETERS, minRegionPeers: 10, minTikPeers: 5 });
    const slice = summarizeAnalysis(result, points.slice(0, 10));
    expect(slice.protocols).toBe(10);
    expect(slice.scoredProtocols + slice.unscoredProtocols).toBe(10);
  });
});
