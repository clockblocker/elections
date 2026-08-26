from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

CRAWLER = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(CRAWLER))

from decode_script_result import decode_script_tables
from historical import (
    _uik_number,
    build_sample_requests,
    classify_page,
    extract_direct_protocol,
    live_official_url,
    make_plan,
    spec_requests,
)
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

    def test_historical_uik_label_without_number_sign(self):
        self.assertEqual(_uik_number("УИК №1325"), 1325)
        self.assertEqual(_uik_number("УИК  3117"), 3117)

    def test_decoder_reaches_tables_after_resize_handler(self):
        source = """
        <table class="table-striped first"><tr><td class="one">x</td></tr></table>
        <table class="table-striped second"><tr><td class="two">y</td></tr></table>
        <script>
        var repair = function(name,value,table){
          var cells = table.getElementsByClassName(name);
          for (var i = 0; i < cells.length; i++) { cells[i].innerHTML = value; }
        };
        var a = function(){
          var first = document.getElementsByClassName('first')[0];
          repair('one', '1', first);
          window.addEventListener('resize', function(){ repair('one', '1', first); });
          var second = document.getElementsByClassName('second')[0];
          repair('two', '2', second);
        };
        document.addEventListener('DOMContentLoaded', a);
        </script>
        """
        self.assertEqual(decode_script_tables(source), [[["1"]], [["2"]]])

    def test_replacement_call_can_span_a_formatting_newline(self):
        source = """<html><table class='table table-striped qz'><tr><td class='x'>obfuscated</td></tr></table><script>
        var zzRandom = function(a,b,c){var x=document.getElementsByClassName(a); x[0].innerHTML = b;};
        var qz = 1; var a = function(){zzRandom('x', '
        39', qz);}; a();</script></html>"""
        self.assertEqual(decode_script_tables(source)[0][0][0], "39")


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

    def test_new_shared_cooldown_invalidates_a_reserved_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            limiter = SharedRateLimiter(
                10,
                coordination_dir=Path(directory),
                clock=clock.monotonic,
            )
            limiter.wait()
            first_sleep = True

            def sleep(seconds):
                nonlocal first_sleep
                if first_sleep:
                    first_sleep = False
                    limiter.penalize(2)
                clock.sleep(seconds)

            limiter.sleep = sleep
            self.assertEqual(limiter.wait(), 2)
            self.assertEqual(clock.value, 2)


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

    def test_archive_capture_is_mapped_to_live_legacy_host(self):
        request = {
            "url": (
                "https://web.archive.org/web/20111211011923id_/"
                "http://www.vybory.izbirkom.ru/region/izbirkom?type=242"
            )
        }
        self.assertEqual(
            live_official_url(request),
            "http://old.izbirkom.ru/region/izbirkom?type=242",
        )

    def test_explicit_live_url_and_source_modes_are_preserved(self):
        request = {
            "year": 1993,
            "url": "https://web.archive.org/web/1id_/http://cikrf.ru/old.html",
            "live_url": "http://www.cikrf.ru/current.php",
        }
        live = spec_requests([request], "live-only")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["url"], request["live_url"])
        both = spec_requests([request], "archive-fallback")
        self.assertEqual(
            [row["source_variant"] for row in both],
            ["live-official", "archived-official"],
        )
        self.assertEqual(both[1]["url"], request["url"])

    def test_live_only_request_is_omitted_from_archive_plan(self):
        request = {"year": 2016, "url": "http://old.izbirkom.ru/tvdTree?vrn=1"}
        self.assertEqual(spec_requests([request], "archive-only"), [])
        self.assertEqual(len(spec_requests([request], "archive-fallback")), 1)

    def test_sample_requests_use_preserved_hierarchy_ids(self):
        payload = json.dumps(
            [
                {
                    "id": "uik-tvd",
                    "text": "УИК №7",
                    "href": (
                        "http://old.izbirkom.ru/region/region/izbirkom?"
                        "root=tik-root&tvd=uik-tvd&vrn=election-vrn&"
                        "region=1&sub_region=1"
                    ),
                    "isUik": True,
                }
            ],
            ensure_ascii=False,
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = root / "sha256" / "fixture"
            body.parent.mkdir(parents=True)
            body.write_bytes(payload)
            observations = [
                {
                    "request_class": "gas-hierarchy-children",
                    "expected_granularity": "uik-navigation",
                    "entity_tvd": "tik-tvd",
                    "status": 200,
                    "body_path": "sha256/fixture",
                    "url": "http://old.izbirkom.ru/tree",
                    "sha256": "0" * 64,
                }
            ]
            selections = [
                {
                    "year": 2007,
                    "election_vrn": "election-vrn",
                    "pronetvd": "null",
                    "region_label": "Region",
                    "tik_label": "TIK",
                    "tik_tvd": "tik-tvd",
                    "contests": [
                        {"contest": "party", "direct_type": 242, "column_type": 233}
                    ],
                }
            ]
            requests = build_sample_requests(selections, observations, root, 3)
        self.assertEqual(len(requests), 3)
        direct = next(
            row for row in requests if row["request_class"] == "gas-uik-direct-protocol"
        )
        self.assertEqual(direct["uik_tvd"], "uik-tvd")
        self.assertIn("root=tik-root", direct["url"])
        self.assertIn("type=242", direct["url"])


if __name__ == "__main__":
    unittest.main()
