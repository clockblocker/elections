import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from election_research.peer_clt_v2 import Parameters, Point, analyze


def point(
    identifier: str,
    option_votes: int,
    valid_ballots: int = 1_000,
    turnout: int = 50,
    region: str = "1",
    tik: str = "tik-1",
) -> Point:
    return Point(
        id=identifier,
        region_code=region,
        tik_tvd=tik,
        registered_voters=2_000,
        valid_ballots=valid_ballots,
        ballots_counted=20 * turnout,
        option_votes=option_votes,
    )


def fixture() -> list[Point]:
    return [
        point(
            f"p{index}",
            195 + (index % 7) * 2,
            turnout=35 + index % 20,
            tik=f"tik-{index // 20}",
        )
        for index in range(80)
    ]


class PeerCltV2Tests(unittest.TestCase):
    def test_scores_every_protocol_or_retains_a_reason(self) -> None:
        points = fixture() + [point("zero", 0, valid_ballots=0, turnout=0)]
        result = analyze(points, Parameters(min_region_peers=10, min_tik_peers=5))
        self.assertEqual(result.protocols, len(points))
        self.assertEqual(result.scored_protocols + result.unscored_protocols, len(points))
        self.assertEqual(result.estimates[-1].status, "unscored")
        self.assertEqual(result.estimates[-1].reason, "no-valid-ballots")

    def test_expected_count_is_n_times_peer_share_and_anomaly_ranks_higher(self) -> None:
        points = fixture() + [
            point("moderate", 260, turnout=45),
            point("large", 500, turnout=45),
        ]
        result = analyze(points, Parameters(min_region_peers=10, min_tik_peers=5))
        by_id = {item.id: item for item in result.estimates}
        moderate = by_id["moderate"]
        large = by_id["large"]
        self.assertAlmostEqual(moderate.expected_votes or 0, (moderate.expected_share or 0) * 1_000)
        self.assertGreater(large.p_sus or 0, moderate.p_sus or 0)
        self.assertEqual(large.grade, "P1")

    def test_overdispersion_widens_the_predictive_interval(self) -> None:
        points = [
            Point(
                **{
                    **item.__dict__,
                    "option_votes": 300 if index % 2 else 100,
                }
            )
            for index, item in enumerate(fixture())
        ]
        result = analyze(points, Parameters(min_region_peers=10, min_tik_peers=5))
        estimate = {item.id: item for item in result.estimates}["p20"]
        self.assertGreater(estimate.overdispersion or 0, 1)
        self.assertGreater((estimate.interval95 or (0, 0))[1] - (estimate.interval95 or (0, 0))[0], 30)

    def test_calibrates_the_displayed_interval_to_empirical_coverage(self) -> None:
        result = analyze(fixture(), Parameters(min_region_peers=10, min_tik_peers=5))
        scored = [item for item in result.estimates if item.status == "scored"]
        covered = sum(
            bool(item.interval95)
            and (item.interval95 or (0, 0))[0] <= item.observed_votes <= (item.interval95 or (0, 0))[1]
            for item in scored
        ) / len(scored)
        self.assertGreaterEqual(covered, 0.94)
        self.assertGreaterEqual(result.interval_calibration_scale, 1)


if __name__ == "__main__":
    unittest.main()
