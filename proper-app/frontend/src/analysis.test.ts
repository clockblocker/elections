import { describe, expect, test } from "vitest";
import { analyze } from "./analysis";
import type { Point } from "./types";

const point = (id: string, regionCode: string, ballotsCounted: number, validBallots: number, optionVotes: number): Point => ({
  id, regionCode, ballotsCounted, validBallots, optionVotes, registeredVoters: 100,
  turnout: ballotsCounted, result: 100 * optionVotes / validBallots,
  uikNumber: Number(id.replace(/\D/g, "")) || 1, uikTvd: id, tikTvd: regionCode,
  tikName: regionCode, regionName: regionCode
});

describe("shpilkin-odds-v1", () => {
  test("matches the Python regional baseline reference case", () => {
    const result = analyze([
      point("a1", "1", 40, 40, 10), point("a2", "1", 80, 80, 50),
      point("b1", "2", 40, 40, 20), point("b2", "2", 80, 80, 45)
    ], { referenceTurnoutMin: 30, referenceTurnoutMax: 50, analysisTurnoutMin: 50, positiveExcessOnly: true });
    expect(result.referencePoints).toBe(2);
    expect(result.estimatedExcessVotes).toBeCloseTo(50);
    expect(result.estimates.get("a2")?.expectedVotes).toBeCloseTo(10);
    expect(result.estimates.get("b2")?.expectedVotes).toBeCloseTo(35);
  });
});
