from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

CRAWLER = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(CRAWLER))

from historical import classify_page, extract_direct_protocol, make_plan
from shared_rate import SharedRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, seconds)


class HistoricalFamilyTests(unittest.TestCase):
    def fixture_payload(self) -> bytes:
        return (
            (FIXTURES / "historical_plain.html")
            .read_text(encoding="utf-8")
            .encode("windows-1251")
        )

    def test_windows_1251_plain_table_family(self):
        result = classify_page(self.fixture_payload())
        self.assertEqual(result["family"], "plain-html-table")
        self.assertEqual(result["encoding"], "windows-1251")
        self.assertTrue(result["has_voter_accounting"])
        self.assertTrue(result["has_party_signal"])

    def test_direct_protocol_preserves_historical_labels(self):
        result = extract_direct_protocol(self.fixture_payload())
        self.assertEqual(result["commission_name"], "Тестовая")
        self.assertEqual(
            result["accounting"]["Число избирателей, внесенных в список избирателей"],
            10,
        )
        self.assertEqual(result["votes"]["1. Политическая партия А"], 4)
        self.assertTrue(result["validation"]["vote_sum_matches_valid_ballots"])

    def test_binary_and_error_families(self):
        self.assertEqual(
            classify_page(bytes.fromhex("D0CF11E0A1B11AE1") + b"xls")["family"],
            "ole-compound-spreadsheet",
        )
        error = classify_page(b"<html><h1>Service Unavailable</h1></html>")
        self.assertTrue(error["challenge_or_error"])


class SharedCoordinationTests(unittest.TestCase):
    def test_independent_instances_share_smooth_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            one = SharedRateLimiter(
                10,
                coordination_dir=Path(directory),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            two = SharedRateLimiter(
                10,
                coordination_dir=Path(directory),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            self.assertEqual(one.wait(), 0)
            self.assertAlmostEqual(two.wait(), 0.1)
            self.assertAlmostEqual(clock.value, 0.1)

    def test_cooldown_is_visible_to_another_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            one = SharedRateLimiter(
                10,
                coordination_dir=Path(directory),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            two = SharedRateLimiter(
                10,
                coordination_dir=Path(directory),
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            one.penalize(2)
            self.assertEqual(two.wait(), 2)


class HistoricalPlanTests(unittest.TestCase):
    def test_archive_only_plan_is_non_nationwide(self):
        class Args:
            source_mode = "archive-only"
            rate = 10
            concurrency = 2
            timeout = 20
            retries = 3
            raw_dir = Path("data/raw/test")
            coordination_dir = Path("data/raw/.gas-rate-limit")

        plan = make_plan(
            [{"year": 1995, "archive_urls": ["https://web.archive.org/example"]}],
            Args(),
        )
        self.assertEqual(plan["estimated_requests"], 1)
        self.assertFalse(plan["nationwide_crawl"])
        self.assertEqual(plan["dispatch_interval_ms"], 100)

    def test_catalog_and_probe_spec_have_every_requested_year(self):
        catalog = json.loads(
            (CRAWLER / "historical-elections.json").read_text(encoding="utf-8")
        )
        spec = json.loads(
            (CRAWLER / "historical-probes.json").read_text(encoding="utf-8")
        )
        expected = {1993, 1995, 1999, 2003, 2007, 2011, 2016}
        self.assertEqual({row["year"] for row in catalog["elections"]}, expected)
        self.assertEqual({row["year"] for row in spec["requests"]}, expected)


if __name__ == "__main__":
    unittest.main()
