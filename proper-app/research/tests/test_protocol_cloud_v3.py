import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from election_research.protocol_cloud_v3 import Point, analyze


def point(
    identifier: str,
    option_votes: int,
    valid_ballots: int = 1_000,
    turnout: int = 40,
    region: str = "1",
    tik: str = "tik-1",
    registered_voters: int = 3_000,
) -> Point:
    return Point(
        id=identifier,
        region_code=region,
        tik_tvd=tik,
        registered_voters=registered_voters,
        valid_ballots=valid_ballots,
        ballots_counted=round(registered_voters * turnout / 100),
        option_votes=option_votes,
    )


def fixture(count: int = 100) -> list[Point]:
    return [
        point(
            f"p{index}",
            180 + (index * 17 % 45),
            turnout=34 + index % 14,
            tik=f"tik-{index // 20}",
        )
        for index in range(count)
    ]


class ProtocolCloudV3Tests(unittest.TestCase):
    def test_preserves_invalid_protocol_as_unscored(self) -> None:
        points = fixture() + [
            point("zero", 0, valid_ballots=0, turnout=0),
            point("impossible", 500, valid_ballots=1_000, turnout=20),
        ]

        result = analyze(points)
        zero = result.estimates[-2]
        impossible = result.estimates[-1]

        self.assertEqual(result.protocols, len(points))
        self.assertEqual(len(result.estimates), len(points))
        self.assertEqual(result.scored_protocols + result.unscored_protocols, len(points))
        self.assertEqual(zero.status, "unscored")
        self.assertEqual(zero.grade, "U")
        self.assertEqual(zero.reason, "no-valid-ballots")
        self.assertEqual(impossible.status, "unscored")
        self.assertEqual(impossible.grade, "U")
        self.assertEqual(impossible.reason, "invalid-accounting")
        self.assertEqual(result.core.protocols, 50)

    def test_dense_high_turnout_high_result_tail_remains_suspicious(self) -> None:
        points = fixture() + [
            point(f"tail{index}", 990, turnout=99, region="2", tik="tail-tik")
            for index in range(20)
        ]

        result = analyze(points)
        tail = [item for item in result.estimates if item.id.startswith("tail")]

        self.assertTrue(tail)
        self.assertTrue(all(item.grade == "P3" for item in tail))
        self.assertTrue(all(item.direction == "high-high" for item in tail))

    def test_finite_count_variance_distinguishes_one_of_one(self) -> None:
        points = fixture() + [
            point("tiny", 1, valid_ballots=1, turnout=100, region="2", tik="edge", registered_voters=1),
            point(
                "large",
                1_000,
                valid_ballots=1_000,
                turnout=100,
                region="2",
                tik="edge",
                registered_voters=1_000,
            ),
        ]

        result = analyze(points)
        by_id = {item.id: item for item in result.estimates}
        tiny = by_id["tiny"]
        large = by_id["large"]

        self.assertGreater(large.p_sus or 0, tiny.p_sus or 0)
        self.assertEqual(large.grade, "P3")
        self.assertIn("finite-count-clt-weak", tiny.quality_flags)

    def test_returns_finite_election_wide_core_and_contour(self) -> None:
        result = analyze(fixture())
        covariance = result.core.covariance

        self.assertGreater(result.core.expected_turnout, 30)
        self.assertLess(result.core.expected_turnout, 50)
        self.assertGreater(result.core.expected_result, 15)
        self.assertLess(result.core.expected_result, 25)
        self.assertEqual(len(result.core.contour50), 97)
        self.assertEqual(len(result.core.contour95), 97)
        self.assertGreater(covariance[0][0], 0)
        self.assertGreater(covariance[1][1], 0)
        self.assertLessEqual(
            abs(covariance[0][1]),
            0.999 * math.sqrt(covariance[0][0] * covariance[1][1]),
        )
        self.assertTrue(
            all(
                math.isfinite(item["turnout"]) and math.isfinite(item["result"])
                for item in result.core.contour95
            )
        )


if __name__ == "__main__":
    unittest.main()
