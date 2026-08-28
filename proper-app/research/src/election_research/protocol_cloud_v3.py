"""Reference implementation of the election-wide protocol-cloud screen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import cos, exp, isfinite, log, pi, sin, sqrt
from statistics import median

METHOD_SLUG = "protocol-cloud-clt-v3"
METHOD_VERSION = 3
CHI_SQUARE_2_95 = 5.991464547107979
CHI_SQUARE_2_50 = 1.3862943611198906
Z_95 = 1.959963984540054
NORMAL_MAD = 0.6744897501960817


@dataclass(frozen=True)
class Parameters:
    core_fraction: float = 0.5
    core_iterations: int = 8
    covariance_ridge: float = 1e-4
    fdr_threshold: float = 0.05
    review_threshold: float = 0.999

    def validate(self) -> None:
        if not all(isfinite(value) for value in asdict(self).values()):
            raise ValueError("protocol-cloud parameters must be finite")
        if not 0.25 <= self.core_fraction <= 0.75:
            raise ValueError("core fraction must be in [0.25, 0.75]")
        if not 1 <= self.core_iterations <= 30:
            raise ValueError("core iterations must be in [1, 30]")
        if not 0 < self.covariance_ridge <= 0.1:
            raise ValueError("invalid covariance ridge")
        if not 0 < self.fdr_threshold < 1 or not 0.9 <= self.review_threshold < 1:
            raise ValueError("invalid review threshold")


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

    @property
    def result(self) -> float | None:
        return 100 * self.option_votes / self.valid_ballots if self.valid_ballots > 0 else None


@dataclass(frozen=True)
class PointEstimate:
    id: str
    status: str
    reason: str | None
    grade: str
    observed_votes: int
    observed_share: float | None
    baseline_source: str
    core_protocols: int
    core_ballots: int
    expected_turnout: float | None = None
    expected_share: float | None = None
    expected_votes: float | None = None
    residual_votes: float | None = None
    standard_error_votes: float | None = None
    interval95: tuple[float, float] | None = None
    z_score: float | None = None
    p_value: float | None = None
    q_value: float | None = None
    p_sus: float | None = None
    variance_ratio: float | None = None
    mahalanobis_squared: float | None = None
    direction: str | None = None
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Core:
    protocols: int
    expected_turnout: float
    expected_result: float
    covariance: tuple[tuple[float, float], tuple[float, float]]
    contour50: tuple[dict[str, float], ...]
    contour95: tuple[dict[str, float], ...]


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
    core: Core
    estimates: tuple[PointEstimate, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Observation:
    point: Point
    turnout_logit: float
    result_logit: float


@dataclass(frozen=True)
class _Fit:
    center: tuple[float, float]
    covariance: tuple[tuple[float, float], tuple[float, float]]
    members: tuple[_Observation, ...]


def _invalid_reason(point: Point) -> str | None:
    if point.registered_voters <= 0:
        return "no-registered-voters"
    if point.valid_ballots <= 0:
        return "no-valid-ballots"
    if (
        point.ballots_counted < 0
        or point.ballots_counted > point.registered_voters
        or point.valid_ballots > point.ballots_counted
        or point.option_votes < 0
        or point.option_votes > point.valid_ballots
    ):
        return "invalid-accounting"
    return None


def _logit_count(successes: int, total: int) -> float:
    return log((successes + 0.5) / (total - successes + 0.5))


def _inverse_logit(value: float) -> float:
    if value >= 0:
        exponential = exp(-value)
        return 1 / (1 + exponential)
    exponential = exp(value)
    return exponential / (1 + exponential)


def _inverse2(
    matrix: tuple[tuple[float, float], tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    determinant = max(1e-12, matrix[0][0] * matrix[1][1] - matrix[0][1] ** 2)
    return (
        (matrix[1][1] / determinant, -matrix[0][1] / determinant),
        (-matrix[0][1] / determinant, matrix[0][0] / determinant),
    )


def _distance(
    observation: _Observation,
    center: tuple[float, float],
    inverse: tuple[tuple[float, float], tuple[float, float]],
) -> float:
    x = observation.turnout_logit - center[0]
    y = observation.result_logit - center[1]
    return max(
        0,
        x * (inverse[0][0] * x + inverse[0][1] * y)
        + y * (inverse[1][0] * x + inverse[1][1] * y),
    )


def _consistency_multiplier(fraction: float) -> float:
    cutoff = -2 * log(1 - fraction)
    truncated_mean = 2 - cutoff * (1 - fraction) / fraction
    return 2 / max(0.05, truncated_mean)


def _covariance(
    observations: tuple[_Observation, ...],
    center: tuple[float, float],
    multiplier: float,
    ridge: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    xx = xy = yy = 0.0
    for observation in observations:
        x = observation.turnout_logit - center[0]
        y = observation.result_logit - center[1]
        xx += x * x
        xy += x * y
        yy += y * y
    denominator = max(1, len(observations) - 1)
    return (
        (multiplier * xx / denominator + ridge, multiplier * xy / denominator),
        (multiplier * xy / denominator, multiplier * yy / denominator + ridge),
    )


def _fit_core(observations: list[_Observation], parameters: Parameters) -> _Fit:
    count = max(20, int(len(observations) * parameters.core_fraction))
    center = (
        median(item.turnout_logit for item in observations),
        median(item.result_logit for item in observations),
    )
    turnout_mad = median(abs(item.turnout_logit - center[0]) for item in observations) / NORMAL_MAD
    result_mad = median(abs(item.result_logit - center[1]) for item in observations) / NORMAL_MAD
    matrix = (
        (max(parameters.covariance_ridge, turnout_mad**2), 0.0),
        (0.0, max(parameters.covariance_ridge, result_mad**2)),
    )
    members: tuple[_Observation, ...] = tuple(observations[:count])
    multiplier = _consistency_multiplier(parameters.core_fraction)
    for _ in range(parameters.core_iterations):
        inverse = _inverse2(matrix)
        members = tuple(
            sorted(
                observations,
                key=lambda item: (_distance(item, center, inverse), item.point.id),
            )[:count]
        )
        center = (
            sum(item.turnout_logit for item in members) / len(members),
            sum(item.result_logit for item in members) / len(members),
        )
        matrix = _covariance(members, center, multiplier, parameters.covariance_ridge)

    # The trimmed covariance is measured on observed protocol logits, so its
    # diagonal already contains ordinary finite-count noise. Subtract the
    # core's average delta-method sampling variance to estimate latent
    # between-protocol spread. Each scored protocol adds its own sampling
    # variance back below.
    expected_turnout = _inverse_logit(center[0])
    expected_result = _inverse_logit(center[1])
    average_turnout_sampling = sum(
        1
        / max(
            1e-9,
            item.point.registered_voters * expected_turnout * (1 - expected_turnout),
        )
        for item in members
    ) / len(members)
    average_result_sampling = sum(
        1
        / max(
            1e-9,
            item.point.valid_ballots * expected_result * (1 - expected_result),
        )
        for item in members
    ) / len(members)
    turnout_variance = max(
        parameters.covariance_ridge,
        matrix[0][0] - average_turnout_sampling,
    )
    result_variance = max(
        parameters.covariance_ridge,
        matrix[1][1] - average_result_sampling,
    )
    covariance_limit = 0.999 * sqrt(turnout_variance * result_variance)
    cross_covariance = max(-covariance_limit, min(covariance_limit, matrix[0][1]))
    covariance = (
        (turnout_variance, cross_covariance),
        (cross_covariance, result_variance),
    )
    return _Fit(center, covariance, members)


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


def _direction(dx: float, dy: float, distance: float) -> str:
    if distance < CHI_SQUARE_2_95:
        return "central"
    if dx >= 0 and dy >= 0:
        return "high-high"
    if dx >= 0:
        return "high-low"
    if dy >= 0:
        return "low-high"
    return "low-low"


def _contour(fit: _Fit, squared_radius: float) -> tuple[dict[str, float], ...]:
    a = max(1e-12, fit.covariance[0][0])
    l11 = sqrt(a)
    l21 = fit.covariance[1][0] / l11
    l22 = sqrt(max(1e-12, fit.covariance[1][1] - l21**2))
    radius = sqrt(squared_radius)
    return tuple(
        {
            "turnout": 100 * _inverse_logit(fit.center[0] + radius * l11 * cos(2 * pi * index / 96)),
            "result": 100
            * _inverse_logit(
                fit.center[1]
                + radius
                * (l21 * cos(2 * pi * index / 96) + l22 * sin(2 * pi * index / 96))
            ),
        }
        for index in range(97)
    )


def _unscored(point: Point, reason: str) -> PointEstimate:
    return PointEstimate(
        id=point.id,
        status="unscored",
        reason=reason,
        grade="U",
        observed_votes=point.option_votes,
        observed_share=point.option_votes / point.valid_ballots if point.valid_ballots > 0 else None,
        baseline_source="none",
        core_protocols=0,
        core_ballots=0,
        quality_flags=(reason,),
    )


def analyze(points: list[Point], parameters: Parameters | None = None) -> Analysis:
    parameters = parameters or Parameters()
    parameters.validate()
    estimates: dict[str, PointEstimate] = {}
    observations: list[_Observation] = []
    for point in points:
        reason = _invalid_reason(point)
        if reason:
            estimates[point.id] = _unscored(point, reason)
        else:
            observations.append(
                _Observation(
                    point,
                    _logit_count(point.ballots_counted, point.registered_voters),
                    _logit_count(point.option_votes, point.valid_ballots),
                )
            )
    if len(observations) < 20:
        raise ValueError("at least 20 usable protocols are required")

    fit = _fit_core(observations, parameters)
    expected_turnout = _inverse_logit(fit.center[0])
    expected_result = _inverse_logit(fit.center[1])
    core_ballots = sum(item.point.valid_ballots for item in fit.members)
    p_values: list[tuple[str, float]] = []
    for observation in observations:
        point = observation.point
        sampling_turnout = 1 / max(
            1e-9, point.registered_voters * expected_turnout * (1 - expected_turnout)
        )
        sampling_result = 1 / max(
            1e-9, point.valid_ballots * expected_result * (1 - expected_result)
        )
        predictive = (
            (fit.covariance[0][0] + sampling_turnout, fit.covariance[0][1]),
            (fit.covariance[1][0], fit.covariance[1][1] + sampling_result),
        )
        dx = observation.turnout_logit - fit.center[0]
        dy = observation.result_logit - fit.center[1]
        distance = _distance(observation, fit.center, _inverse2(predictive))
        p_value = max(2.220446049250313e-16, exp(-distance / 2))
        p_sus = 1 - p_value
        result_sigma = sqrt(predictive[1][1])
        interval_share = (
            _inverse_logit(fit.center[1] - Z_95 * result_sigma),
            _inverse_logit(fit.center[1] + Z_95 * result_sigma),
        )
        expected_votes = point.valid_ballots * expected_result
        residual_votes = point.option_votes - expected_votes
        weak_finite_count_approximation = min(
            point.registered_voters * expected_turnout,
            point.registered_voters * (1 - expected_turnout),
            point.valid_ballots * expected_result,
            point.valid_ballots * (1 - expected_result),
        ) < 10
        p_values.append((point.id, p_value))
        estimates[point.id] = PointEstimate(
            id=point.id,
            status="scored",
            reason=None,
            grade=_grade(p_sus),
            observed_votes=point.option_votes,
            observed_share=point.option_votes / point.valid_ballots,
            baseline_source="election-wide-robust-core",
            core_protocols=len(fit.members),
            core_ballots=core_ballots,
            expected_turnout=100 * expected_turnout,
            expected_share=expected_result,
            expected_votes=expected_votes,
            residual_votes=residual_votes,
            standard_error_votes=point.valid_ballots
            * expected_result
            * (1 - expected_result)
            * result_sigma,
            interval95=(
                point.valid_ballots * interval_share[0],
                point.valid_ballots * interval_share[1],
            ),
            z_score=dy / result_sigma,
            p_value=p_value,
            p_sus=p_sus,
            variance_ratio=predictive[1][1] / max(1e-12, sampling_result),
            mahalanobis_squared=distance,
            direction=_direction(dx, dy, distance),
            quality_flags=(
                "election-wide-robust-core",
                "protocol-level-logit-clt",
                "finite-count-correction",
                *(("finite-count-clt-weak",) if weak_finite_count_approximation else ()),
            ),
        )
    for identifier, q_value in _adjust_by(p_values).items():
        estimates[identifier] = replace(estimates[identifier], q_value=q_value)

    scored = [item for item in estimates.values() if item.status == "scored"]
    flagged = [item for item in scored if (item.p_sus or 0) >= parameters.review_threshold]
    core = Core(
        protocols=len(fit.members),
        expected_turnout=100 * expected_turnout,
        expected_result=100 * expected_result,
        covariance=fit.covariance,
        contour50=_contour(fit, CHI_SQUARE_2_50),
        contour95=_contour(fit, CHI_SQUARE_2_95),
    )
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
        flagged_residual_votes=sum(max(0, item.residual_votes or 0) for item in flagged),
        core=core,
        estimates=tuple(estimates[point.id] for point in points),
    )
