import type {
  AnalysisParameters, AnalysisResult, AnalysisSummary, Point, PointEstimate
} from "./types";

export const METHOD = { slug: "protocol-cloud-clt-v3", version: 3 } as const;

export const DEFAULT_PARAMETERS: AnalysisParameters = {
  coreFraction: 0.5,
  coreIterations: 8,
  covarianceRidge: 1e-4,
  fdrThreshold: 0.05,
  reviewThreshold: 0.999
};

interface Observation {
  point: Point;
  turnoutLogit: number;
  resultLogit: number;
}

interface CoreFit {
  center: [number, number];
  covariance: [[number, number], [number, number]];
  members: Observation[];
}

const CHI_SQUARE_2_50 = 1.3862943611198906;
const CHI_SQUARE_2_95 = 5.991464547107979;
const Z_95 = 1.959963984540054;
const NORMAL_MAD = 0.6744897501960817;

function validate(parameters: AnalysisParameters): void {
  if (!Object.values(parameters).every(Number.isFinite)
    || parameters.coreFraction < 0.25 || parameters.coreFraction > 0.75
    || !Number.isInteger(parameters.coreIterations) || parameters.coreIterations < 1
    || parameters.coreIterations > 30 || parameters.covarianceRidge <= 0
    || parameters.covarianceRidge > 0.1
    || parameters.fdrThreshold <= 0 || parameters.fdrThreshold >= 1
    || parameters.reviewThreshold < 0.9 || parameters.reviewThreshold >= 1) {
    throw new Error("Invalid protocol-cloud parameters");
  }
}

function invalidReason(point: Point): PointEstimate["reason"] | null {
  if (point.registeredVoters <= 0) return "no-registered-voters";
  if (point.validBallots <= 0) return "no-valid-ballots";
  if (point.turnout === null || point.result === null
    || !Number.isFinite(point.turnout) || !Number.isFinite(point.result)
    || point.ballotsCounted < 0 || point.ballotsCounted > point.registeredVoters
    || point.validBallots > point.ballotsCounted
    || point.optionVotes < 0 || point.optionVotes > point.validBallots) return "invalid-accounting";
  return null;
}

function logitCount(successes: number, total: number): number {
  return Math.log((successes + 0.5) / (total - successes + 0.5));
}

function inverseLogit(value: number): number {
  if (value >= 0) {
    const exponential = Math.exp(-value);
    return 1 / (1 + exponential);
  }
  const exponential = Math.exp(value);
  return exponential / (1 + exponential);
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function covariance(
  observations: Observation[], center: [number, number], multiplier: number, ridge: number
): [[number, number], [number, number]] {
  let xx = 0; let xy = 0; let yy = 0;
  for (const observation of observations) {
    const dx = observation.turnoutLogit - center[0];
    const dy = observation.resultLogit - center[1];
    xx += dx * dx; xy += dx * dy; yy += dy * dy;
  }
  const denominator = Math.max(1, observations.length - 1);
  return [
    [multiplier * xx / denominator + ridge, multiplier * xy / denominator],
    [multiplier * xy / denominator, multiplier * yy / denominator + ridge]
  ];
}

function inverse2(matrix: [[number, number], [number, number]]): [[number, number], [number, number]] {
  const determinant = Math.max(1e-12, matrix[0][0] * matrix[1][1] - matrix[0][1] ** 2);
  return [
    [matrix[1][1] / determinant, -matrix[0][1] / determinant],
    [-matrix[0][1] / determinant, matrix[0][0] / determinant]
  ];
}

function distanceSquared(
  observation: Pick<Observation, "turnoutLogit" | "resultLogit">,
  center: [number, number], inverse: [[number, number], [number, number]]
): number {
  const x = observation.turnoutLogit - center[0];
  const y = observation.resultLogit - center[1];
  return Math.max(0, x * (inverse[0][0] * x + inverse[0][1] * y)
    + y * (inverse[1][0] * x + inverse[1][1] * y));
}

function coreConsistencyMultiplier(fraction: number): number {
  // For a bivariate Gaussian, D² ~ chi-square(2), an exponential distribution.
  // Correct the covariance lost by retaining only the central `fraction` of D².
  const cutoff = -2 * Math.log(1 - fraction);
  const truncatedMean = 2 - cutoff * (1 - fraction) / fraction;
  return 2 / Math.max(0.05, truncatedMean);
}

function robustSeed(observations: Observation[]): [number, number] {
  return [
    median(observations.map((item) => item.turnoutLogit)),
    median(observations.map((item) => item.resultLogit))
  ];
}

export function fitRobustCore(observations: Observation[], parameters: AnalysisParameters): CoreFit {
  const count = Math.max(20, Math.floor(observations.length * parameters.coreFraction));
  let center = robustSeed(observations);
  const turnoutMad = median(observations.map((item) => Math.abs(item.turnoutLogit - center[0]))) / NORMAL_MAD;
  const resultMad = median(observations.map((item) => Math.abs(item.resultLogit - center[1]))) / NORMAL_MAD;
  let matrix: [[number, number], [number, number]] = [
    [Math.max(parameters.covarianceRidge, turnoutMad ** 2), 0],
    [0, Math.max(parameters.covarianceRidge, resultMad ** 2)]
  ];
  let members = observations.slice(0, count);
  const multiplier = coreConsistencyMultiplier(parameters.coreFraction);
  for (let iteration = 0; iteration < parameters.coreIterations; iteration += 1) {
    const inverse = inverse2(matrix);
    members = [...observations]
      .sort((left, right) => distanceSquared(left, center, inverse) - distanceSquared(right, center, inverse)
        || left.point.id.localeCompare(right.point.id))
      .slice(0, count);
    center = [
      members.reduce((sum, item) => sum + item.turnoutLogit, 0) / members.length,
      members.reduce((sum, item) => sum + item.resultLogit, 0) / members.length
    ];
    matrix = covariance(members, center, multiplier, parameters.covarianceRidge);
  }
  // The trimmed covariance above is measured on observed protocol logits and
  // therefore already contains ordinary finite-count noise. Remove the core's
  // average delta-method sampling variance to estimate between-protocol spread;
  // each protocol's own sampling variance is added back when it is scored.
  const expectedTurnout = inverseLogit(center[0]);
  const expectedResult = inverseLogit(center[1]);
  const averageTurnoutSampling = members.reduce((sum, item) => sum + 1 / Math.max(
    1e-9, item.point.registeredVoters * expectedTurnout * (1 - expectedTurnout)
  ), 0) / members.length;
  const averageResultSampling = members.reduce((sum, item) => sum + 1 / Math.max(
    1e-9, item.point.validBallots * expectedResult * (1 - expectedResult)
  ), 0) / members.length;
  const turnoutVariance = Math.max(parameters.covarianceRidge, matrix[0][0] - averageTurnoutSampling);
  const resultVariance = Math.max(parameters.covarianceRidge, matrix[1][1] - averageResultSampling);
  const covarianceLimit = 0.999 * Math.sqrt(turnoutVariance * resultVariance);
  const crossCovariance = Math.max(-covarianceLimit, Math.min(covarianceLimit, matrix[0][1]));
  return {
    center,
    covariance: [[turnoutVariance, crossCovariance], [crossCovariance, resultVariance]],
    members
  };
}

export function adjustBenjaminiHochberg(values: Array<{ id: string; p: number }>): Map<string, number> {
  const sorted = [...values].sort((left, right) => left.p - right.p || left.id.localeCompare(right.id));
  const result = new Map<string, number>();
  let next = 1;
  for (let index = sorted.length - 1; index >= 0; index -= 1) {
    const adjusted = Math.min(next, sorted[index].p * sorted.length / (index + 1), 1);
    next = adjusted; result.set(sorted[index].id, adjusted);
  }
  return result;
}

export function adjustBenjaminiYekutieli(values: Array<{ id: string; p: number }>): Map<string, number> {
  const harmonic = values.reduce((sum, _, index) => sum + 1 / (index + 1), 0);
  return adjustBenjaminiHochberg(values.map((item) => ({ ...item, p: Math.min(1, item.p * harmonic) })));
}

export function gradeFor(pSus: number | null): PointEstimate["grade"] {
  if (pSus === null) return "U";
  if (pSus >= 0.999) return "P3";
  if (pSus >= 0.99) return "P2";
  if (pSus >= 0.95) return "P1";
  return "P0";
}

function unscored(point: Point, reason: PointEstimate["reason"]): PointEstimate {
  return {
    status: "unscored", reason, grade: "U", observedVotes: point.optionVotes,
    observedShare: point.validBallots > 0 ? point.optionVotes / point.validBallots : null,
    baselineSource: "none", peerPrecincts: 0, peerBallots: 0,
    expectedTurnout: null, expectedShare: null, expectedVotes: null, residualVotes: null,
    standardErrorVotes: null, interval95: null, zScore: null, pValue: null,
    qValue: null, pSus: null, overdispersion: null, mahalanobisSquared: null,
    direction: null, qualityFlags: [reason ?? "unscored"]
  };
}

function direction(dx: number, dy: number, distance: number): PointEstimate["direction"] {
  if (distance < CHI_SQUARE_2_95) return "central";
  if (dx >= 0 && dy >= 0) return "high-high";
  if (dx >= 0) return "high-low";
  if (dy >= 0) return "low-high";
  return "low-low";
}

function contour(fit: CoreFit, squaredRadius: number): Array<{ turnout: number; result: number }> {
  const a = Math.max(1e-12, fit.covariance[0][0]);
  const l11 = Math.sqrt(a);
  const l21 = fit.covariance[1][0] / l11;
  const l22 = Math.sqrt(Math.max(1e-12, fit.covariance[1][1] - l21 ** 2));
  const radius = Math.sqrt(squaredRadius);
  return Array.from({ length: 97 }, (_, index) => {
    const angle = 2 * Math.PI * index / 96;
    const cosine = Math.cos(angle); const sine = Math.sin(angle);
    return {
      turnout: 100 * inverseLogit(fit.center[0] + radius * l11 * cosine),
      result: 100 * inverseLogit(fit.center[1] + radius * (l21 * cosine + l22 * sine))
    };
  });
}

export function summarizeAnalysis(
  result: AnalysisResult, points: Point[], reviewThreshold = result.parameters.reviewThreshold
): AnalysisSummary {
  let scoredProtocols = 0; let unscoredProtocols = 0; let flaggedProtocols = 0;
  let observedVotes = 0; let expectedVotes = 0; let flaggedResidualVotes = 0;
  for (const point of points) {
    const estimate = result.estimates.get(point.id);
    if (!estimate || estimate.status === "unscored" || estimate.expectedVotes === null) {
      unscoredProtocols += 1; continue;
    }
    scoredProtocols += 1; observedVotes += point.optionVotes; expectedVotes += estimate.expectedVotes;
    if ((estimate.pSus ?? 0) >= reviewThreshold) {
      flaggedProtocols += 1; flaggedResidualVotes += Math.max(0, estimate.residualVotes ?? 0);
    }
  }
  return { protocols: points.length, scoredProtocols, unscoredProtocols, flaggedProtocols,
    observedVotes, expectedVotes, flaggedResidualVotes };
}

export function analyze(points: Point[], parameters: AnalysisParameters = DEFAULT_PARAMETERS): AnalysisResult {
  validate(parameters);
  const estimates = new Map<string, PointEstimate>();
  const observations: Observation[] = [];
  for (const point of points) {
    const reason = invalidReason(point);
    if (reason) { estimates.set(point.id, unscored(point, reason)); continue; }
    observations.push({
      point,
      turnoutLogit: logitCount(point.ballotsCounted, point.registeredVoters),
      resultLogit: logitCount(point.optionVotes, point.validBallots)
    });
  }
  if (observations.length < 20) throw new Error("At least 20 usable protocols are required.");
  const fit = fitRobustCore(observations, parameters);
  const expectedTurnoutShare = inverseLogit(fit.center[0]);
  const expectedResultShare = inverseLogit(fit.center[1]);
  const coreBallots = fit.members.reduce((sum, item) => sum + item.point.validBallots, 0);
  const pValues: Array<{ id: string; p: number }> = [];

  for (const observation of observations) {
    const { point } = observation;
    const samplingTurnout = 1 / Math.max(1e-9,
      point.registeredVoters * expectedTurnoutShare * (1 - expectedTurnoutShare));
    const samplingResult = 1 / Math.max(1e-9,
      point.validBallots * expectedResultShare * (1 - expectedResultShare));
    const predictive: [[number, number], [number, number]] = [
      [fit.covariance[0][0] + samplingTurnout, fit.covariance[0][1]],
      [fit.covariance[1][0], fit.covariance[1][1] + samplingResult]
    ];
    const dx = observation.turnoutLogit - fit.center[0];
    const dy = observation.resultLogit - fit.center[1];
    const squared = distanceSquared(observation, fit.center, inverse2(predictive));
    const pValue = Math.max(Number.EPSILON, Math.exp(-squared / 2));
    const pSus = 1 - pValue;
    const resultSigma = Math.sqrt(predictive[1][1]);
    const intervalShare: [number, number] = [
      inverseLogit(fit.center[1] - Z_95 * resultSigma),
      inverseLogit(fit.center[1] + Z_95 * resultSigma)
    ];
    const expectedVotes = point.validBallots * expectedResultShare;
    const residualVotes = point.optionVotes - expectedVotes;
    const weakFiniteCountApproximation = Math.min(
      point.registeredVoters * expectedTurnoutShare,
      point.registeredVoters * (1 - expectedTurnoutShare),
      point.validBallots * expectedResultShare,
      point.validBallots * (1 - expectedResultShare)
    ) < 10;
    pValues.push({ id: point.id, p: pValue });
    estimates.set(point.id, {
      status: "scored", grade: gradeFor(pSus), observedVotes: point.optionVotes,
      observedShare: point.optionVotes / point.validBallots,
      baselineSource: "election-wide-robust-core", peerPrecincts: fit.members.length,
      peerBallots: coreBallots, expectedTurnout: 100 * expectedTurnoutShare,
      expectedShare: expectedResultShare, expectedVotes, residualVotes,
      standardErrorVotes: point.validBallots * expectedResultShare * (1 - expectedResultShare) * resultSigma,
      interval95: [point.validBallots * intervalShare[0], point.validBallots * intervalShare[1]],
      zScore: dy / resultSigma, pValue, qValue: null, pSus,
      overdispersion: predictive[1][1] / Math.max(1e-12, samplingResult),
      mahalanobisSquared: squared, direction: direction(dx, dy, squared),
      qualityFlags: ["election-wide-robust-core", "protocol-level-logit-clt", "finite-count-correction",
        ...(weakFiniteCountApproximation ? ["finite-count-clt-weak"] : [])]
    });
  }
  for (const [id, qValue] of adjustBenjaminiYekutieli(pValues)) {
    const estimate = estimates.get(id); if (estimate) estimate.qValue = qValue;
  }
  const result: AnalysisResult = {
    method: METHOD, parameters: { ...parameters }, protocols: points.length,
    scoredProtocols: 0, unscoredProtocols: 0, flaggedProtocols: 0,
    observedVotes: 0, expectedVotes: 0, flaggedResidualVotes: 0,
    core: {
      protocols: fit.members.length,
      expectedTurnout: 100 * expectedTurnoutShare,
      expectedResult: 100 * expectedResultShare,
      covariance: fit.covariance,
      contour50: contour(fit, CHI_SQUARE_2_50),
      contour95: contour(fit, CHI_SQUARE_2_95)
    },
    estimates
  };
  return Object.assign(result, summarizeAnalysis(result, points));
}
