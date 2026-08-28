import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from election_research.shpilkin_v1 import Parameters, Point, analyze  # noqa: E402


class ShpilkinV1Tests(unittest.TestCase):
    def test_uses_regional_reference_odds_and_positive_excess(self) -> None:
        points = [
            Point("a-reference", "1", 100, 40, 40, 10),
            Point("a-high", "1", 100, 80, 80, 50),
            Point("b-reference", "2", 100, 40, 40, 20),
            Point("b-high", "2", 100, 80, 80, 45),
        ]
        result = analyze(points, Parameters(reference_turnout_min=30, reference_turnout_max=50))

        self.assertEqual(result.reference_points, 2)
        self.assertEqual(result.analyzed_points, 2)
        self.assertEqual(result.regions_with_local_baseline, 2)
        self.assertAlmostEqual(result.estimates[0].expected_votes, 10)
        self.assertAlmostEqual(result.estimates[0].excess_votes, 40)
        self.assertAlmostEqual(result.estimates[1].expected_votes, 35)
        self.assertAlmostEqual(result.estimates[1].excess_votes, 10)
        self.assertAlmostEqual(result.estimated_excess_votes, 50)

    def test_falls_back_to_pooled_reference(self) -> None:
        points = [
            Point("reference", "1", 100, 40, 40, 10),
            Point("other-region", "2", 100, 80, 80, 40),
        ]
        result = analyze(points)
        self.assertEqual(result.estimates[0].baseline_source, "pooled")
        self.assertAlmostEqual(result.estimates[0].expected_votes, 40 / 3)

    def test_rejects_an_empty_reference_band(self) -> None:
        with self.assertRaisesRegex(ValueError, "reference turnout band"):
            analyze([Point("high", "1", 100, 90, 90, 60)])


if __name__ == "__main__":
    unittest.main()
