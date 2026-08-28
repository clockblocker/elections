"""Versioned reference implementation of the physical-UIK excess-vote model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite

METHOD_SLUG = "shpilkin-odds-v1"
METHOD_VERSION = 1


@dataclass(frozen=True)
class Parameters:
    reference_turnout_min: float = 20.0
    reference_turnout_max: float = 50.0
    analysis_turnout_min: float = 50.0
    positive_excess_only: bool = True

    def validate(self) -> None:
        values = (
            self.reference_turnout_min,
            self.reference_turnout_max,
            self.analysis_turnout_min,
        )
        if not all(isfinite(value) and 0 <= value <= 100 for value in values):
            raise ValueError("turnout parameters must be finite percentages from 0 to 100")
        if self.reference_turnout_min >= self.reference_turnout_max:
            raise ValueError("reference turnout minimum must be below its maximum")


@dataclass(frozen=True)
class Point:
    id: str
    region_code: str
    registered_voters: int
    valid_ballots: int
    ballots_counted: int
    option_votes: int

    @property
    def turnout(self) -> float:
        return 100 * self.ballots_counted / self.registered_voters

    @property
    def other_votes(self) -> int:
        return self.valid_ballots - self.option_votes

    def validate(self) -> None:
        if self.registered_voters <= 0:
            raise ValueError(f"point {self.id} has no registered voters")
        if min(self.valid_ballots, self.ballots_counted, self.option_votes) < 0:
            raise ValueError(f"point {self.id} has a negative count")
        if self.option_votes > self.valid_ballots:
            raise ValueError(f"point {self.id} has more target votes than valid ballots")


@dataclass(frozen=True)
class PointEstimate:
    id: str
    baseline_source: str
    baseline_odds: float
    expected_votes: float
    excess_votes: float


@dataclass(frozen=True)
class Analysis:
    method: str
    version: int
    parameters: dict[str, object]
    points: int
    reference_points: int
    analyzed_points: int
    regions_with_local_baseline: int
    baseline_odds: float
    baseline_share: float
    observed_votes: int
    expected_votes: float
    estimated_excess_votes: float
    estimates: tuple[PointEstimate, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _odds(points: list[Point]) -> float | None:
    target = sum(point.option_votes for point in points)
    other = sum(point.other_votes for point in points)
    return target / other if other > 0 else None


def analyze(points: list[Point], parameters: Parameters = Parameters()) -> Analysis:
    """Estimate positive target votes above regional low-turnout baseline odds.

    The comparison fixes each analyzed precinct's observed non-target valid votes and
    applies target-to-other odds learned from physical precincts in the reference band.
    A region uses its own baseline when possible and the pooled baseline otherwise.
    """

    parameters.validate()
    for point in points:
        point.validate()

    reference = [
        point
        for point in points
        if parameters.reference_turnout_min <= point.turnout < parameters.reference_turnout_max
    ]
    pooled_odds = _odds(reference)
    if pooled_odds is None:
        raise ValueError("reference turnout band contains no usable non-target votes")

    reference_by_region: dict[str, list[Point]] = {}
    for point in reference:
        reference_by_region.setdefault(point.region_code, []).append(point)
    regional_odds = {
        region: odds
        for region, region_points in reference_by_region.items()
        if (odds := _odds(region_points)) is not None
    }

    eligible = [point for point in points if point.turnout >= parameters.analysis_turnout_min]
    estimates: list[PointEstimate] = []
    expected_total = 0.0
    excess_total = 0.0
    for point in eligible:
        odds = regional_odds.get(point.region_code, pooled_odds)
        expected = odds * point.other_votes
        raw_excess = point.option_votes - expected
        excess = max(0.0, raw_excess) if parameters.positive_excess_only else raw_excess
        expected_total += expected
        excess_total += excess
        estimates.append(
            PointEstimate(
                id=point.id,
                baseline_source=(
                    f"region:{point.region_code}"
                    if point.region_code in regional_odds
                    else "pooled"
                ),
                baseline_odds=odds,
                expected_votes=expected,
                excess_votes=excess,
            )
        )

    return Analysis(
        method=METHOD_SLUG,
        version=METHOD_VERSION,
        parameters=asdict(parameters),
        points=len(points),
        reference_points=len(reference),
        analyzed_points=len(eligible),
        regions_with_local_baseline=len(regional_odds),
        baseline_odds=pooled_odds,
        baseline_share=pooled_odds / (1 + pooled_odds),
        observed_votes=sum(point.option_votes for point in eligible),
        expected_votes=expected_total,
        estimated_excess_votes=excess_total,
        estimates=tuple(estimates),
    )
