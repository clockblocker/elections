import type {
  AnalysisParameters, AnalysisResult, AnalysisSummary, Point, PointEstimate
} from "./types";

export const METHOD = { slug: "peer-clt-v2", version: 2 } as const;

export const DEFAULT_PARAMETERS: AnalysisParameters = {
  turnoutBinWidth: 2.5,
  turnoutWindow: 10,
  minTikPeers: 8,
  minRegionPeers: 30,
  tikPriorBallots: 2_000,
  regionPriorBallots: 10_000,
  dispersionPriorPoints: 30,
  fdrThreshold: 0.05
};

interface Bin {
  votes: number;
  ballots: number;
  points: number;
}

interface PeerAggregate extends Bin {
  source: string;
}

interface Prediction {
  point: Point;
  source: string;
  peerPrecincts: number;
  peerBallots: number;
  effectiveBallots: number;
  expectedShare: number;
  residualShare: number;
  measurementVariance: number;
  qualityFlags: string[];
}

interface ResidualObservation {
  residual: number;
  measurementVariance: number;
}

const NORMAL_MAD = 0.6744897501960817;
const Z_95 = 1.959963984540054;

function validPoint(point: Point): boolean {
  return point.registeredVoters > 0 && point.validBallots > 0
    && Number.isFinite(point.turnout) && point.turnout !== null
    && point.ballotsCounted >= 0 && point.ballotsCounted <= point.registeredVoters
    && point.optionVotes >= 0 && point.optionVotes <= point.validBallots;
}

function validate(parameters: AnalysisParameters): void {
  const finite = Object.values(parameters).every((value) => Number.isFinite(value));
  if (!finite
    || parameters.turnoutBinWidth <= 0 || parameters.turnoutBinWidth > 20
    || parameters.turnoutWindow < parameters.turnoutBinWidth || parameters.turnoutWindow > 50
    || !Number.isInteger(parameters.minTikPeers) || parameters.minTikPeers < 2
    || !Number.isInteger(parameters.minRegionPeers) || parameters.minRegionPeers < parameters.minTikPeers
    || parameters.tikPriorBallots <= 0 || parameters.regionPriorBallots <= 0
    || parameters.dispersionPriorPoints <= 0
    || parameters.fdrThreshold <= 0 || parameters.fdrThreshold >= 1) {
    throw new Error("Invalid peer-CLT parameters");
  }
}

function binsFor(index: Map<string, Bin[]>, key: string, count: number): Bin[] {
  const current = index.get(key);
  if (current) return current;
  const bins = Array.from({ length: count }, () => ({ votes: 0, ballots: 0, points: 0 }));
  index.set(key, bins);
  return bins;
}

function addToIndex(index: Map<string, Bin[]>, key: string, bin: number, point: Point, count: number): void {
  const target = binsFor(index, key, count)[bin];
  target.votes += point.optionVotes;
  target.ballots += point.validBallots;
  target.points += 1;
}

function nearby(
  index: Map<string, Bin[]>, key: string, point: Point, parameters: AnalysisParameters
): PeerAggregate {
  const bins = index.get(key) ?? [];
  const turnout = point.turnout ?? 0;
  const ownBin = Math.min(bins.length - 1, Math.max(0, Math.floor(turnout / parameters.turnoutBinWidth)));
  let votes = 0;
  let ballots = 0;
  let points = 0;
  for (let bin = 0; bin < bins.length; bin += 1) {
    const center = (bin + 0.5) * parameters.turnoutBinWidth;
    const distance = Math.abs(center - turnout);
    const weight = Math.max(0, 1 - distance / (parameters.turnoutWindow + parameters.turnoutBinWidth / 2));
    if (weight === 0) continue;
    const item = bins[bin];
    const subtract = bin === ownBin ? 1 : 0;
    votes += weight * (item.votes - subtract * point.optionVotes);
    ballots += weight * (item.ballots - subtract * point.validBallots);
    points += weight * (item.points - subtract);
  }
  return { votes: Math.max(0, votes), ballots: Math.max(0, ballots), points: Math.max(0, points), source: key };
}

function allPeers(index: Map<string, Bin[]>, key: string, point: Point): PeerAggregate {
  const bins = index.get(key) ?? [];
  let votes = -point.optionVotes;
  let ballots = -point.validBallots;
  let points = -1;
  for (const bin of bins) {
    votes += bin.votes;
    ballots += bin.ballots;
    points += bin.points;
  }
  return { votes: Math.max(0, votes), ballots: Math.max(0, ballots), points: Math.max(0, points), source: key };
}

function boundedShare(votes: number, ballots: number): number {
  return Math.min(1 - 1e-9, Math.max(1e-9, (votes + 0.5) / (ballots + 1)));
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function robustHeterogeneity(observations: ResidualObservation[]): number | null {
  if (observations.length < 5) return null;
  const center = median(observations.map((item) => item.residual));
  const mad = median(observations.map((item) => Math.abs(item.residual - center)));
  const totalVariance = (mad / NORMAL_MAD) ** 2;
  const measurement = median(observations.map((item) => item.measurementVariance));
  return Math.max(0, totalVariance - measurement);
}

// Abramowitz-Stegun 7.1.26; absolute error below 1.5e-7.
function normalCdf(value: number): number {
  if (value <= -8) return 0;
  if (value >= 8) return 1;
  const sign = value < 0 ? -1 : 1;
  const x = Math.abs(value) / Math.sqrt(2);
  const t = 1 / (1 + 0.3275911 * x);
  const erf = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return 0.5 * (1 + sign * erf);
}

export function adjustBenjaminiHochberg(values: Array<{ id: string; p: number }>): Map<string, number> {
  const sorted = [...values].sort((left, right) => left.p - right.p || left.id.localeCompare(right.id));
  const result = new Map<string, number>();
  let next = 1;
  for (let index = sorted.length - 1; index >= 0; index -= 1) {
    const adjusted = Math.min(next, sorted[index].p * sorted.length / (index + 1), 1);
    next = adjusted;
    result.set(sorted[index].id, adjusted);
  }
  return result;
}

export function adjustBenjaminiYekutieli(values: Array<{ id: string; p: number }>): Map<string, number> {
  const harmonic = values.reduce((sum, _, index) => sum + 1 / (index + 1), 0);
  const scaled = values.map((item) => ({ ...item, p: Math.min(1, item.p * harmonic) }));
  return adjustBenjaminiHochberg(scaled);
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
    expectedShare: null, expectedVotes: null, residualVotes: null,
    standardErrorVotes: null, interval95: null, zScore: null, pValue: null,
    qValue: null, pSus: null, overdispersion: null, qualityFlags: [reason ?? "unscored"]
  };
}

function invalidReason(point: Point): PointEstimate["reason"] | null {
  if (point.registeredVoters <= 0) return "no-registered-voters";
  if (point.validBallots <= 0) return "no-valid-ballots";
  if (point.turnout === null || !Number.isFinite(point.turnout)
    || point.ballotsCounted < 0 || point.ballotsCounted > point.registeredVoters
    || point.optionVotes < 0 || point.optionVotes > point.validBallots) return "invalid-accounting";
  return null;
}

export function summarizeAnalysis(
  result: AnalysisResult, points: Point[], fdrThreshold = result.parameters.fdrThreshold
): AnalysisSummary {
  let scoredProtocols = 0;
  let unscoredProtocols = 0;
  let flaggedProtocols = 0;
  let observedVotes = 0;
  let expectedVotes = 0;
  let flaggedResidualVotes = 0;
  for (const point of points) {
    const estimate = result.estimates.get(point.id);
    if (!estimate || estimate.status === "unscored" || estimate.expectedVotes === null) {
      unscoredProtocols += 1;
      continue;
    }
    scoredProtocols += 1;
    observedVotes += point.optionVotes;
    expectedVotes += estimate.expectedVotes;
    if (estimate.qValue !== null && estimate.qValue <= fdrThreshold && (estimate.residualVotes ?? 0) > 0) {
      flaggedProtocols += 1;
      flaggedResidualVotes += estimate.residualVotes ?? 0;
    }
  }
  return {
    protocols: points.length, scoredProtocols, unscoredProtocols, flaggedProtocols,
    observedVotes, expectedVotes, flaggedResidualVotes
  };
}

export function analyze(points: Point[], parameters: AnalysisParameters = DEFAULT_PARAMETERS): AnalysisResult {
  validate(parameters);
  const estimates = new Map<string, PointEstimate>();
  const usable = points.filter((point) => {
    const reason = invalidReason(point);
    if (reason) estimates.set(point.id, unscored(point, reason));
    return reason === null && validPoint(point);
  });
  if (!usable.length) throw new Error("No protocols have usable ballot counts for the peer model.");

  const binCount = Math.ceil(100 / parameters.turnoutBinWidth) + 1;
  const national = new Map<string, Bin[]>();
  const regions = new Map<string, Bin[]>();
  const tiks = new Map<string, Bin[]>();
  for (const point of usable) {
    const bin = Math.min(binCount - 1, Math.max(0, Math.floor((point.turnout ?? 0) / parameters.turnoutBinWidth)));
    addToIndex(national, "national", bin, point, binCount);
    addToIndex(regions, point.regionCode, bin, point, binCount);
    addToIndex(tiks, `${point.regionCode}:${point.tikTvd}`, bin, point, binCount);
  }

  const predictions: Prediction[] = [];
  for (const point of usable) {
    let nationalPeers = nearby(national, "national", point, parameters);
    if (nationalPeers.points < parameters.minRegionPeers || nationalPeers.ballots <= 0) {
      nationalPeers = allPeers(national, "national", point);
    }
    if (nationalPeers.ballots <= 0) {
      estimates.set(point.id, unscored(point, "no-peer-model"));
      continue;
    }
    const nationalShare = boundedShare(nationalPeers.votes, nationalPeers.ballots);

    const regionPeers = nearby(regions, point.regionCode, point, parameters);
    const hasRegion = regionPeers.points >= parameters.minRegionPeers && regionPeers.ballots > 0;
    const regionShare = hasRegion
      ? boundedShare(
        regionPeers.votes + parameters.regionPriorBallots * nationalShare,
        regionPeers.ballots + parameters.regionPriorBallots
      )
      : nationalShare;

    const tikKey = `${point.regionCode}:${point.tikTvd}`;
    const tikPeers = nearby(tiks, tikKey, point, parameters);
    const hasTik = tikPeers.points >= parameters.minTikPeers && tikPeers.ballots > 0;
    const expectedShare = hasTik
      ? boundedShare(
        tikPeers.votes + parameters.tikPriorBallots * regionShare,
        tikPeers.ballots + parameters.tikPriorBallots
      )
      : regionShare;
    const sourcePeers = hasTik ? tikPeers : hasRegion ? regionPeers : nationalPeers;
    const effectiveBallots = hasTik
      ? tikPeers.ballots + parameters.tikPriorBallots
      : hasRegion ? regionPeers.ballots + parameters.regionPriorBallots : nationalPeers.ballots;
    const qualityFlags: string[] = [];
    if (!hasTik) qualityFlags.push("sparse-tik-fallback");
    if (!hasRegion) qualityFlags.push("sparse-region-fallback");
    const observedShare = point.optionVotes / point.validBallots;
    predictions.push({
      point,
      source: hasTik ? `tik:${point.tikTvd}` : hasRegion ? `region:${point.regionCode}` : "national",
      peerPrecincts: sourcePeers.points,
      peerBallots: sourcePeers.ballots,
      effectiveBallots,
      expectedShare,
      residualShare: observedShare - expectedShare,
      measurementVariance: expectedShare * (1 - expectedShare)
        * (1 / point.validBallots + 1 / Math.max(1, effectiveBallots)),
      qualityFlags
    });
  }

  const residuals = (items: Prediction[]): ResidualObservation[] => items.map((item) => ({
    residual: item.residualShare, measurementVariance: item.measurementVariance
  }));
  const globalTau = robustHeterogeneity(residuals(predictions)) ?? 0;
  const byRegion = new Map<string, Prediction[]>();
  const byTik = new Map<string, Prediction[]>();
  for (const prediction of predictions) {
    const regionItems = byRegion.get(prediction.point.regionCode) ?? [];
    regionItems.push(prediction); byRegion.set(prediction.point.regionCode, regionItems);
    const tikKey = `${prediction.point.regionCode}:${prediction.point.tikTvd}`;
    const tikItems = byTik.get(tikKey) ?? [];
    tikItems.push(prediction); byTik.set(tikKey, tikItems);
  }
  const regionTau = new Map<string, number>();
  for (const [region, items] of byRegion) {
    const local = robustHeterogeneity(residuals(items));
    const weight = local === null ? 0 : items.length / (items.length + parameters.dispersionPriorPoints);
    regionTau.set(region, weight * (local ?? globalTau) + (1 - weight) * globalTau);
  }
  const tikTau = new Map<string, number>();
  for (const [tik, items] of byTik) {
    const local = robustHeterogeneity(residuals(items));
    const parent = regionTau.get(items[0].point.regionCode) ?? globalTau;
    const weight = local === null ? 0 : items.length / (items.length + parameters.dispersionPriorPoints);
    tikTau.set(tik, weight * (local ?? parent) + (1 - weight) * parent);
  }

  const pValues: Array<{ id: string; p: number }> = [];
  for (const prediction of predictions) {
    const { point, expectedShare } = prediction;
    const expectedVotes = point.validBallots * expectedShare;
    const binomialVariance = point.validBallots * expectedShare * (1 - expectedShare);
    const modelVariance = point.validBallots ** 2 * expectedShare * (1 - expectedShare)
      / Math.max(1, prediction.effectiveBallots);
    const tau = tikTau.get(`${point.regionCode}:${point.tikTvd}`)
      ?? regionTau.get(point.regionCode) ?? globalTau;
    const variance = Math.max(1e-9, binomialVariance + modelVariance + point.validBallots ** 2 * tau);
    const standardErrorVotes = Math.sqrt(variance);
    const residualVotes = point.optionVotes - expectedVotes;
    const zScore = residualVotes / standardErrorVotes;
    const lower = Math.max(0, expectedVotes - Z_95 * standardErrorVotes);
    const upper = Math.min(point.validBallots, expectedVotes + Z_95 * standardErrorVotes);
    const overdispersion = variance / Math.max(1e-9, binomialVariance);
    const cltEligible = expectedVotes >= 10 && point.validBallots - expectedVotes >= 10;
    if (!cltEligible) {
      estimates.set(point.id, {
        ...unscored(point, "clt-small-expected-count"),
        baselineSource: prediction.source, peerPrecincts: prediction.peerPrecincts,
        peerBallots: prediction.peerBallots, expectedShare, expectedVotes, residualVotes,
        standardErrorVotes, interval95: [lower, upper], zScore, overdispersion,
        qualityFlags: [...prediction.qualityFlags, "clt-small-expected-count"]
      });
      continue;
    }
    const continuityZ = (point.optionVotes - 0.5 - expectedVotes) / standardErrorVotes;
    const pValue = Math.max(Number.EPSILON, 1 - normalCdf(continuityZ));
    pValues.push({ id: point.id, p: pValue });
    estimates.set(point.id, {
      status: "scored", grade: "P0", observedVotes: point.optionVotes,
      observedShare: point.optionVotes / point.validBallots,
      baselineSource: prediction.source, peerPrecincts: prediction.peerPrecincts,
      peerBallots: prediction.peerBallots, expectedShare, expectedVotes, residualVotes,
      standardErrorVotes, interval95: [lower, upper], zScore, pValue, qValue: null,
      pSus: null, overdispersion,
      qualityFlags: [...prediction.qualityFlags, "leave-one-out-empirical-calibration"]
    });
  }

  const adjusted = adjustBenjaminiYekutieli(pValues);
  for (const [id, qValue] of adjusted) {
    const estimate = estimates.get(id);
    if (estimate) estimate.qValue = qValue;
  }

  // Calibrate the individual severity independently from multiple testing. The score
  // is the conservative percentile of the positive, leave-one-out CLT residual among
  // all scored physical UIKs; ties receive the lower percentile.
  const calibrated = [...estimates.entries()]
    .filter((entry): entry is [string, PointEstimate] => entry[1].status === "scored" && entry[1].zScore !== null)
    .sort((left, right) => (left[1].zScore ?? 0) - (right[1].zScore ?? 0) || left[0].localeCompare(right[0]));
  let lower = 0;
  while (lower < calibrated.length) {
    let upper = lower + 1;
    const score = calibrated[lower][1].zScore;
    while (upper < calibrated.length && calibrated[upper][1].zScore === score) upper += 1;
    const pSus = lower / calibrated.length;
    for (let index = lower; index < upper; index += 1) {
      calibrated[index][1].pSus = pSus;
      calibrated[index][1].grade = gradeFor(pSus);
    }
    lower = upper;
  }

  const result: AnalysisResult = {
    method: METHOD, parameters: { ...parameters }, protocols: points.length,
    scoredProtocols: 0, unscoredProtocols: 0, flaggedProtocols: 0,
    observedVotes: 0, expectedVotes: 0, flaggedResidualVotes: 0, estimates
  };
  return Object.assign(result, summarizeAnalysis(result, points));
}
