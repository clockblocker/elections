import { describe, expect, test } from "vitest";
import {
  DEFAULT_PARAMETERS, adjustBenjaminiHochberg, adjustBenjaminiYekutieli,
  analyze, gradeFor, summarizeAnalysis
} from "./analysis";
import type { Point } from "./types";

function point(
  id: string, optionVotes: number, validBallots = 1_000, turnout = 40,
  regionCode = "1", tikTvd = "tik-1", registeredVoters = 3_000
): Point {
  const ballotsCounted = Math.round(registeredVoters * turnout / 100);
  return {
    id, regionCode, tikTvd, optionVotes, validBallots, turnout,
    registeredVoters, ballotsCounted,
    result: validBallots > 0 ? 100 * optionVotes / validBallots : null,
    uikNumber: Number(id.replace(/\D/g, "")) || 1, uikTvd: id,
    tikName: tikTvd, regionName: regionCode
  };
}

function fixture(count = 100): Point[] {
  return Array.from({ length: count }, (_, index) => {
    const turnout = 34 + index % 14;
    const votes = 180 + (index * 17 % 45);
    return point(`p${index}`, votes, 1_000, turnout, "1", `tik-${Math.floor(index / 20)}`);
  });
}

describe("protocol-cloud-clt-v3", () => {
  test("models the complete election protocol field and preserves invalid rows as U", () => {
    const points = [...fixture(), point("zero", 0, 0, 0),
      point("impossible", 500, 1_000, 20)];
    const result = analyze(points);
    expect(result.protocols).toBe(points.length);
    expect(result.estimates.size).toBe(points.length);
    expect(result.estimates.get("zero")).toMatchObject({ status: "unscored", grade: "U", reason: "no-valid-ballots" });
    expect(result.estimates.get("impossible")).toMatchObject({
      status: "unscored", grade: "U", reason: "invalid-accounting"
    });
    expect(result.scoredProtocols + result.unscoredProtocols).toBe(points.length);
    expect(result.core.protocols).toBe(50);
  });

  test("does not condition away a dense high-turnout/high-result tail", () => {
    const points = [...fixture(100), ...Array.from({ length: 20 }, (_, index) =>
      point(`tail${index}`, 990, 1_000, 99, "2", "tail-tik"))];
    const result = analyze(points);
    const tail = points.filter((item) => item.id.startsWith("tail"))
      .map((item) => result.estimates.get(item.id)!);
    expect(tail.every((estimate) => estimate.grade === "P3")).toBe(true);
    expect(tail.every((estimate) => estimate.direction === "high-high")).toBe(true);
  });

  test("finite-count variance distinguishes one-of-one from a large 100% protocol", () => {
    const points = [...fixture(),
      point("tiny", 1, 1, 100, "2", "edge", 1),
      point("large", 1_000, 1_000, 100, "2", "edge", 1_000)];
    const result = analyze(points);
    const tiny = result.estimates.get("tiny")!;
    const large = result.estimates.get("large")!;
    expect(large.pSus ?? 0).toBeGreaterThan(tiny.pSus ?? 0);
    expect(large.grade).toBe("P3");
    expect(tiny.qualityFlags).toContain("finite-count-clt-weak");
  });

  test("returns a finite election-wide core and contour", () => {
    const result = analyze(fixture());
    expect(result.core.expectedTurnout).toBeGreaterThan(30);
    expect(result.core.expectedTurnout).toBeLessThan(50);
    expect(result.core.expectedResult).toBeGreaterThan(15);
    expect(result.core.expectedResult).toBeLessThan(25);
    expect(result.core.contour50).toHaveLength(97);
    expect(result.core.contour95).toHaveLength(97);
    const center = { turnout: result.core.expectedTurnout, result: result.core.expectedResult };
    const radialDistance = (item: { turnout: number; result: number }) =>
      Math.hypot(item.turnout - center.turnout, item.result - center.result);
    expect(Math.max(...result.core.contour50.map(radialDistance)))
      .toBeLessThan(Math.max(...result.core.contour95.map(radialDistance)));
    expect(result.core.contour95.every((item) => Number.isFinite(item.turnout) && Number.isFinite(item.result))).toBe(true);
  });

  test("uses known BH and BY adjustments", () => {
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

  test("keeps grades stable when geography is only a display slice", () => {
    expect(gradeFor(0.949)).toBe("P0");
    expect(gradeFor(0.95)).toBe("P1");
    expect(gradeFor(0.99)).toBe("P2");
    expect(gradeFor(0.999)).toBe("P3");
    expect(gradeFor(null)).toBe("U");
    const points = fixture();
    const result = analyze(points);
    const before = result.estimates.get(points[0].id)?.pSus;
    const slice = summarizeAnalysis(result, points.slice(0, 10));
    expect(slice.protocols).toBe(10);
    expect(result.estimates.get(points[0].id)?.pSus).toBe(before);
  });
});
