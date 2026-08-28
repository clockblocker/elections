import type { AnalysisParameters, AnalysisResult, Point, PointEstimate } from "./types";

function odds(points: Point[]): number | null {
  let target = 0;
  let other = 0;
  for (const point of points) {
    target += point.optionVotes;
    other += point.validBallots - point.optionVotes;
  }
  return other > 0 ? target / other : null;
}

export function analyze(points: Point[], parameters: AnalysisParameters): AnalysisResult {
  if (!Number.isFinite(parameters.referenceTurnoutMin) || !Number.isFinite(parameters.referenceTurnoutMax)
    || !Number.isFinite(parameters.analysisTurnoutMin)
    || parameters.referenceTurnoutMin < 0 || parameters.referenceTurnoutMax > 100
    || parameters.analysisTurnoutMin < 0 || parameters.analysisTurnoutMin > 100
    || parameters.referenceTurnoutMin >= parameters.referenceTurnoutMax) {
    throw new Error("Invalid turnout parameters");
  }
  const reference = points.filter((point) => point.turnout >= parameters.referenceTurnoutMin && point.turnout < parameters.referenceTurnoutMax);
  const pooledOdds = odds(reference);
  if (pooledOdds === null) throw new Error("The reference turnout band has no usable non-target votes.");
  const groups = new Map<string, Point[]>();
  for (const point of reference) {
    const group = groups.get(point.regionCode);
    if (group) group.push(point);
    else groups.set(point.regionCode, [point]);
  }
  const regionalOdds = new Map<string, number>();
  for (const [region, regionPoints] of groups) {
    const value = odds(regionPoints);
    if (value !== null) regionalOdds.set(region, value);
  }
  const estimates = new Map<string, PointEstimate>();
  let observedVotes = 0;
  let expectedVotes = 0;
  let estimatedExcessVotes = 0;
  let analyzedPoints = 0;
  for (const point of points) {
    if (point.turnout < parameters.analysisTurnoutMin) continue;
    const localOdds = regionalOdds.get(point.regionCode);
    const baselineOdds = localOdds ?? pooledOdds;
    const expected = baselineOdds * (point.validBallots - point.optionVotes);
    const difference = point.optionVotes - expected;
    const excess = parameters.positiveExcessOnly ? Math.max(0, difference) : difference;
    estimates.set(point.id, {
      baselineSource: localOdds === undefined ? "pooled" : `region:${point.regionCode}`,
      baselineOdds,
      expectedVotes: expected,
      excessVotes: excess
    });
    observedVotes += point.optionVotes;
    expectedVotes += expected;
    estimatedExcessVotes += excess;
    analyzedPoints += 1;
  }
  return {
    points: points.length,
    referencePoints: reference.length,
    analyzedPoints,
    regionsWithLocalBaseline: regionalOdds.size,
    baselineOdds: pooledOdds,
    baselineShare: pooledOdds / (1 + pooledOdds),
    observedVotes,
    expectedVotes,
    estimatedExcessVotes,
    estimates
  };
}
