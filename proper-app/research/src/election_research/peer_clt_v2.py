"""Reference implementation of the physical-UIK peer-conditioned CLT screen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import erfc, isfinite, sqrt
from statistics import median

METHOD_SLUG = "peer-clt-v2"
METHOD_VERSION = 2
NORMAL_MAD = 0.6744897501960817
Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Parameters:
    turnout_bin_width: float = 2.5
    turnout_window: float = 10.0
    min_tik_peers: int = 8
    min_region_peers: int = 30
    tik_prior_ballots: float = 2_000.0
    region_prior_ballots: float = 10_000.0
    dispersion_prior_points: float = 30.0
    fdr_threshold: float = 0.05
    review_threshold: float = 0.999

    def validate(self) -> None:
        values = (
            self.turnout_bin_width,
            self.turnout_window,
            self.tik_prior_ballots,
            self.region_prior_ballots,
            self.dispersion_prior_points,
            self.fdr_threshold,
            self.review_threshold,
        )
        if not all(isfinite(value) for value in values):
            raise ValueError("peer-CLT parameters must be finite")
        if not 0 < self.turnout_bin_width <= 20:
            raise ValueError("turnout bin width must be in (0, 20]")
        if not self.turnout_bin_width <= self.turnout_window <= 50:
            raise ValueError("turnout window must contain at least one bin and be <= 50")
        if self.min_tik_peers < 2 or self.min_region_peers < self.min_tik_peers:
            raise ValueError("invalid peer support floors")
        if min(self.tik_prior_ballots, self.region_prior_ballots, self.dispersion_prior_points) <= 0:
            raise ValueError("shrinkage parameters must be positive")
        if not 0 < self.fdr_threshold < 1:
            raise ValueError("FDR threshold must be in (0, 1)")
        if not 0.9 <= self.review_threshold < 1:
            raise ValueError("review threshold must be in [0.9, 1)")


@dataclass(frozen=True)
class Point:
    id: str
    region_code: str
    tik_tvd: str
    registered_voters: int
    valid_ballots: int
    ballots_counted: int
    option_votes: int

    @property
    def turnout(self) -> float | None:
        return 100 * self.ballots_counted / self.registered_voters if self.registered_voters > 0 else None


@dataclass(frozen=True)
class PointEstimate:
    id: str
    status: str
    reason: str | None
    grade: str
    observed_votes: int
    observed_share: float | None
    baseline_source: str
    peer_precincts: float
    peer_ballots: float
    expected_share: float | None = None
    expected_votes: float | None = None
    residual_votes: float | None = None
    standard_error_votes: float | None = None
    interval95: tuple[float, float] | None = None
    z_score: float | None = None
    p_value: float | None = None
    q_value: float | None = None
    p_sus: float | None = None
    overdispersion: float | None = None
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Analysis:
    method: str
    version: int
    parameters: dict[str, object]
    protocols: int
    scored_protocols: int
    unscored_protocols: int
    flagged_protocols: int
    observed_votes: int
    expected_votes: float
    flagged_residual_votes: float
    interval_calibration_scale: float
    estimates: tuple[PointEstimate, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _Bin:
    votes: float = 0
    ballots: float = 0
    points: float = 0


@dataclass(frozen=True)
class _Peers:
    votes: float
    ballots: float
    points: float


@dataclass(frozen=True)
class _Prediction:
    point: Point
    source: str
    peer_precincts: float
    peer_ballots: float
    effective_ballots: float
    expected_share: float
    residual_share: float
    measurement_variance: float
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class _Draft:
    prediction: _Prediction
    expected_votes: float
    binomial_variance: float
    variance: float
    residual_votes: float
    raw_standard_error: float
    clt_eligible: bool


def _invalid_reason(point: Point) -> str | None:
    if point.registered_voters <= 0:
        return "no-registered-voters"
    if point.valid_ballots <= 0:
        return "no-valid-ballots"
    if (
        point.ballots_counted < 0
        or point.ballots_counted > point.registered_voters
        or point.option_votes < 0
        or point.option_votes > point.valid_ballots
    ):
        return "invalid-accounting"
    return None


def _unscored(point: Point, reason: str) -> PointEstimate:
    return PointEstimate(
        id=point.id,
        status="unscored",
        reason=reason,
        grade="U",
        observed_votes=point.option_votes,
        observed_share=point.option_votes / point.valid_ballots if point.valid_ballots > 0 else None,
        baseline_source="none",
        peer_precincts=0,
        peer_ballots=0,
        quality_flags=(reason,),
    )


def _add(index: dict[str, list[_Bin]], key: str, bin_index: int, point: Point, count: int) -> None:
    bins = index.setdefault(key, [_Bin() for _ in range(count)])
    bins[bin_index].votes += point.option_votes
    bins[bin_index].ballots += point.valid_ballots
    bins[bin_index].points += 1


def _nearby(
    index: dict[str, list[_Bin]], key: str, point: Point, parameters: Parameters
) -> _Peers:
    bins = index.get(key, [])
    turnout = point.turnout or 0
    own_bin = min(len(bins) - 1, max(0, int(turnout // parameters.turnout_bin_width)))
    votes = ballots = points = 0.0
    for bin_index, item in enumerate(bins):
        center = (bin_index + 0.5) * parameters.turnout_bin_width
        distance = abs(center - turnout)
        weight = max(
            0.0,
            1 - distance / (parameters.turnout_window + parameters.turnout_bin_width / 2),
        )
        if weight == 0:
            continue
        subtract = 1 if bin_index == own_bin else 0
        votes += weight * (item.votes - subtract * point.option_votes)
        ballots += weight * (item.ballots - subtract * point.valid_ballots)
        points += weight * (item.points - subtract)
    return _Peers(max(0, votes), max(0, ballots), max(0, points))


def _all_peers(index: dict[str, list[_Bin]], key: str, point: Point) -> _Peers:
    bins = index.get(key, [])
    return _Peers(
        max(0, sum(item.votes for item in bins) - point.option_votes),
        max(0, sum(item.ballots for item in bins) - point.valid_ballots),
        max(0, sum(item.points for item in bins) - 1),
    )


def _share(votes: float, ballots: float) -> float:
    return min(1 - 1e-9, max(1e-9, (votes + 0.5) / (ballots + 1)))


def _heterogeneity(predictions: list[_Prediction]) -> float | None:
    if len(predictions) < 5:
        return None
    center = median(item.residual_share for item in predictions)
    mad = median(abs(item.residual_share - center) for item in predictions)
    total_variance = (mad / NORMAL_MAD) ** 2
    measurement = median(item.measurement_variance for item in predictions)
    return max(0, total_variance - measurement)


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0.0, probability * (len(ordered) - 1)))
    lower = int(position)
    fraction = position - lower
    upper = ordered[lower + 1] if lower + 1 < len(ordered) else ordered[lower]
    return ordered[lower] + fraction * (upper - ordered[lower])


def _normal_survival(value: float) -> float:
    return max(2.220446049250313e-16, 0.5 * erfc(value / sqrt(2)))


def _adjust_bh(values: list[tuple[str, float]]) -> dict[str, float]:
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    result: dict[str, float] = {}
    next_value = 1.0
    for index in range(len(ordered) - 1, -1, -1):
        identifier, value = ordered[index]
        adjusted = min(next_value, value * len(ordered) / (index + 1), 1)
        next_value = adjusted
        result[identifier] = adjusted
    return result


def _adjust_by(values: list[tuple[str, float]]) -> dict[str, float]:
    harmonic = sum(1 / index for index in range(1, len(values) + 1))
    return _adjust_bh([(identifier, min(1, value * harmonic)) for identifier, value in values])


def _grade(p_sus: float | None) -> str:
    if p_sus is None:
        return "U"
    if p_sus >= 0.999:
        return "P3"
    if p_sus >= 0.99:
        return "P2"
    if p_sus >= 0.95:
        return "P1"
    return "P0"


def analyze(points: list[Point], parameters: Parameters | None = None) -> Analysis:
    parameters = parameters or Parameters()
    parameters.validate()
    estimates: dict[str, PointEstimate] = {}
    usable: list[Point] = []
    for point in points:
        reason = _invalid_reason(point)
        if reason:
            estimates[point.id] = _unscored(point, reason)
        else:
            usable.append(point)
    if not usable:
        raise ValueError("no protocols have usable ballot counts for the peer model")

    bin_count = int(100 / parameters.turnout_bin_width) + 2
    national: dict[str, list[_Bin]] = {}
    regions: dict[str, list[_Bin]] = {}
    tiks: dict[str, list[_Bin]] = {}
    for point in usable:
        bin_index = min(bin_count - 1, max(0, int((point.turnout or 0) // parameters.turnout_bin_width)))
        _add(national, "national", bin_index, point, bin_count)
        _add(regions, point.region_code, bin_index, point, bin_count)
        _add(tiks, f"{point.region_code}:{point.tik_tvd}", bin_index, point, bin_count)

    predictions: list[_Prediction] = []
    for point in usable:
        national_peers = _nearby(national, "national", point, parameters)
        if national_peers.points < parameters.min_region_peers or national_peers.ballots <= 0:
            national_peers = _all_peers(national, "national", point)
        if national_peers.ballots <= 0:
            estimates[point.id] = _unscored(point, "no-peer-model")
            continue
        national_share = _share(national_peers.votes, national_peers.ballots)

        region_peers = _nearby(regions, point.region_code, point, parameters)
        has_region = (
            region_peers.points >= parameters.min_region_peers and region_peers.ballots > 0
        )
        region_share = (
            _share(
                region_peers.votes + parameters.region_prior_ballots * national_share,
                region_peers.ballots + parameters.region_prior_ballots,
            )
            if has_region
            else national_share
        )

        tik_key = f"{point.region_code}:{point.tik_tvd}"
        tik_peers = _nearby(tiks, tik_key, point, parameters)
        has_tik = tik_peers.points >= parameters.min_tik_peers and tik_peers.ballots > 0
        expected_share = (
            _share(
                tik_peers.votes + parameters.tik_prior_ballots * region_share,
                tik_peers.ballots + parameters.tik_prior_ballots,
            )
            if has_tik
            else region_share
        )
        source_peers = tik_peers if has_tik else region_peers if has_region else national_peers
        effective_ballots = (
            tik_peers.ballots + parameters.tik_prior_ballots
            if has_tik
            else region_peers.ballots + parameters.region_prior_ballots
            if has_region
            else national_peers.ballots
        )
        flags = ()
        if not has_tik:
            flags += ("sparse-tik-fallback",)
        if not has_region:
            flags += ("sparse-region-fallback",)
        observed_share = point.option_votes / point.valid_ballots
        predictions.append(
            _Prediction(
                point=point,
                source=f"tik:{point.tik_tvd}" if has_tik else f"region:{point.region_code}" if has_region else "national",
                peer_precincts=source_peers.points,
                peer_ballots=source_peers.ballots,
                effective_ballots=effective_ballots,
                expected_share=expected_share,
                residual_share=observed_share - expected_share,
                measurement_variance=expected_share
                * (1 - expected_share)
                * (1 / point.valid_ballots + 1 / max(1, effective_ballots)),
                quality_flags=flags,
            )
        )

    global_tau = _heterogeneity(predictions) or 0
    by_region: dict[str, list[_Prediction]] = {}
    by_tik: dict[str, list[_Prediction]] = {}
    for prediction in predictions:
        by_region.setdefault(prediction.point.region_code, []).append(prediction)
        by_tik.setdefault(
            f"{prediction.point.region_code}:{prediction.point.tik_tvd}", []
        ).append(prediction)
    region_tau: dict[str, float] = {}
    for region, items in by_region.items():
        local = _heterogeneity(items)
        weight = 0 if local is None else len(items) / (len(items) + parameters.dispersion_prior_points)
        region_tau[region] = weight * (local if local is not None else global_tau) + (1 - weight) * global_tau
    tik_tau: dict[str, float] = {}
    for tik, items in by_tik.items():
        local = _heterogeneity(items)
        parent = region_tau.get(items[0].point.region_code, global_tau)
        weight = 0 if local is None else len(items) / (len(items) + parameters.dispersion_prior_points)
        tik_tau[tik] = weight * (local if local is not None else parent) + (1 - weight) * parent

    drafts: list[_Draft] = []
    for prediction in predictions:
        point = prediction.point
        expected = point.valid_ballots * prediction.expected_share
        binomial_variance = point.valid_ballots * prediction.expected_share * (1 - prediction.expected_share)
        model_variance = (
            point.valid_ballots**2
            * prediction.expected_share
            * (1 - prediction.expected_share)
            / max(1, prediction.effective_ballots)
        )
        tau = tik_tau.get(
            f"{point.region_code}:{point.tik_tvd}", region_tau.get(point.region_code, global_tau)
        )
        variance = max(1e-9, binomial_variance + model_variance + point.valid_ballots**2 * tau)
        residual = point.option_votes - expected
        drafts.append(
            _Draft(
                prediction=prediction,
                expected_votes=expected,
                binomial_variance=binomial_variance,
                variance=variance,
                residual_votes=residual,
                raw_standard_error=sqrt(variance),
                clt_eligible=expected >= 10 and point.valid_ballots - expected >= 10,
            )
        )

    calibration_residuals = [
        abs(draft.residual_votes / draft.raw_standard_error)
        for draft in drafts
        if draft.clt_eligible
    ]
    interval_calibration_scale = max(1, _quantile(calibration_residuals, 0.95) / Z_95)

    p_values: list[tuple[str, float]] = []
    for draft in drafts:
        prediction = draft.prediction
        point = prediction.point
        expected = draft.expected_votes
        residual = draft.residual_votes
        standard_error = draft.raw_standard_error * interval_calibration_scale
        z_score = residual / standard_error
        interval = (
            max(0, expected - Z_95 * standard_error),
            min(point.valid_ballots, expected + Z_95 * standard_error),
        )
        common = {
            "id": point.id,
            "observed_votes": point.option_votes,
            "observed_share": point.option_votes / point.valid_ballots,
            "baseline_source": prediction.source,
            "peer_precincts": prediction.peer_precincts,
            "peer_ballots": prediction.peer_ballots,
            "expected_share": prediction.expected_share,
            "expected_votes": expected,
            "residual_votes": residual,
            "standard_error_votes": standard_error,
            "interval95": interval,
            "z_score": z_score,
            "overdispersion": draft.variance
            * interval_calibration_scale**2
            / max(1e-9, draft.binomial_variance),
        }
        if not draft.clt_eligible:
            estimates[point.id] = PointEstimate(
                **common,
                status="unscored",
                reason="clt-small-expected-count",
                grade="U",
                quality_flags=prediction.quality_flags
                + ("empirical-95-interval-calibration", "clt-small-expected-count"),
            )
            continue
        p_value = _normal_survival((point.option_votes - 0.5 - expected) / standard_error)
        p_values.append((point.id, p_value))
        estimates[point.id] = PointEstimate(
            **common,
            status="scored",
            reason=None,
            grade="P0",
            p_value=p_value,
            quality_flags=prediction.quality_flags
            + ("empirical-95-interval-calibration", "leave-one-out-empirical-calibration"),
        )

    for identifier, q_value in _adjust_by(p_values).items():
        estimates[identifier] = replace(estimates[identifier], q_value=q_value)

    calibrated = sorted(
        (item for item in estimates.values() if item.status == "scored"),
        key=lambda item: (item.z_score if item.z_score is not None else 0, item.id),
    )
    lower = 0
    while lower < len(calibrated):
        upper = lower + 1
        while upper < len(calibrated) and calibrated[upper].z_score == calibrated[lower].z_score:
            upper += 1
        p_sus = lower / len(calibrated)
        for index in range(lower, upper):
            identifier = calibrated[index].id
            estimates[identifier] = replace(estimates[identifier], p_sus=p_sus, grade=_grade(p_sus))
        lower = upper

    scored = [item for item in estimates.values() if item.status == "scored"]
    flagged = [
        item
        for item in scored
        if (item.p_sus or 0) >= parameters.review_threshold
        and (item.residual_votes or 0) > 0
    ]
    return Analysis(
        method=METHOD_SLUG,
        version=METHOD_VERSION,
        parameters=asdict(parameters),
        protocols=len(points),
        scored_protocols=len(scored),
        unscored_protocols=len(points) - len(scored),
        flagged_protocols=len(flagged),
        observed_votes=sum(item.observed_votes for item in scored),
        expected_votes=sum(item.expected_votes or 0 for item in scored),
        flagged_residual_votes=sum(item.residual_votes or 0 for item in flagged),
        interval_calibration_scale=interval_calibration_scale,
        estimates=tuple(estimates[point.id] for point in points),
    )
