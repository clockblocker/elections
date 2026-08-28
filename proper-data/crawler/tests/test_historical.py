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
from generate_historical_typescript import (
    generate as generate_historical_typescript,
)
from generate_historical_typescript import (
    write_types as write_historical_types,
)
from historical import (
    _uik_number,
    build_sample_requests,
    classify_page,
    extract_direct_protocol,
    live_official_url,
    make_plan,
    spec_requests,
)
from historical_nationwide import (
    decoded_rows,
    enriched_relations,
    oik_breadcrumbs,
    report_kind,
    transpose,
)
from pipeline import UIK_RE as NATIONWIDE_UIK_RE
from shared_rate import SharedRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += max(0.0, seconds)


class HistoricalFamilyTests(unittest.TestCase):
    def test_nationwide_typescript_generation_uses_historical_types(self):
        source = {
            "official_url": "http://old.izbirkom.ru/result",
            "sha256": "a" * 64,
            "provenance": "live-official",
        }
        dataset = {
            "election": "2007-duma",
            "contests": {"party": {"tic": 233, "uik": 242}},
            "records": [
                {
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "uik_number": 1,
                    "uik_tvd": "u1",
                    "uik_name": "УИК №1",
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                    "party_accounting": {"Число избирателей": 10},
                    "party_votes": {"1. Партия": 10},
                }
            ],
            "sources": [
                {
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "party": source,
                }
            ],
            "tik_protocols": [
                {
                    "tik_tvd": "t1",
                    "party": {
                        "uik_count": 1,
                        "uik_tvds": ["u1"],
                        "accounting": {"Число избирателей": 10},
                        "votes": {"1. Партия": 10},
                    },
                }
            ],
            "relations": [
                {
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "uik_number": 1,
                    "uik_tvd": "u1",
                    "uik_name": "УИК №1",
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            stale = output / "protocol" / "tic" / "242" / "sample.ts"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            result = generate_historical_typescript(dataset, output)
            types = (output / "protocol" / "types.ts").read_text(encoding="utf-8")
            shard = (
                output / "protocol" / "uik" / "242" / "region-1-part-001.ts"
            ).read_text(encoding="utf-8")
            root = (output / "uik-to-tik" / "region-1.ts").read_text(
                encoding="utf-8"
            )
        self.assertEqual(result["uiks"], 1)
        self.assertIn('election: "2007-duma"', types)
        self.assertIn("regionTvd: string", types)
        self.assertIn('"regionName": "Test Region"', shard)
        self.assertIn('"district": null', root)
        self.assertNotIn('"oikTvd"', root)
        self.assertIn('"sourceReportType": 233', shard)
        self.assertFalse(stale.exists())

    def test_nationwide_typescript_generation_labels_presidential_ballot(self):
        source = {
            "official_url": "http://old.izbirkom.ru/result",
            "sha256": "a" * 64,
            "provenance": "live-official",
        }
        dataset = {
            "election": "2018-president",
            "contests": {"candidate": {"tic": 227, "uik": 226}},
            "records": [
                {
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "uik_number": 1,
                    "uik_tvd": "u1",
                    "uik_name": "УИК №1",
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                    "candidate_accounting": {"Число избирателей": 10},
                    "candidate_votes": {"Путин В.В.": 10},
                }
            ],
            "sources": [
                {
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "candidate": source,
                }
            ],
            "tik_protocols": [
                {
                    "tik_tvd": "t1",
                    "candidate": {
                        "uik_count": 1,
                        "uik_tvds": ["u1"],
                        "accounting": {"Число избирателей": 10},
                        "votes": {"Путин В.В.": 10},
                    },
                }
            ],
            "relations": [
                {
                    "region": "1",
                    "region_code": "1",
                    "region_tvd": "r1",
                    "region_name": "Test Region",
                    "uik_number": 1,
                    "uik_tvd": "u1",
                    "uik_name": "УИК №1",
                    "tik_tvd": "t1",
                    "tik_name": "Test TIK",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            generate_historical_typescript(dataset, output)
            types = (output / "protocol" / "types.ts").read_text(encoding="utf-8")
            root = (output / "uik-to-tik.ts").read_text(encoding="utf-8")
        self.assertIn('ballot: "presidential"', types)
        self.assertNotIn("district: DistrictRef", types.split("export type PresidentialUikProtocol", 1)[1].split("export type UikProtocol", 1)[0])
        self.assertIn("president_2018_uik_to_tik", root)

    def test_historical_oik_ancestry_requires_single_member_election(self):
        nodes = [
            {"node_id": "cec", "parent_id": None, "text": "ЦИК России", "region": "0"},
            {"node_id": "r1", "parent_id": "cec", "text": "Region", "region": "1"},
            {"node_id": "o1", "parent_id": "r1", "text": "District", "region": "1"},
            {"node_id": "t1", "parent_id": "o1", "text": "TIK", "region": "1"},
            {"node_id": "u1", "parent_id": "t1", "text": "УИК №1", "region": "1", "is_uik": True},
        ]
        party = enriched_relations(
            {"election": "2007-duma", "contests": {"party": {}}, "nodes": nodes}
        )[0]
        single = enriched_relations(
            {
                "election": "2016-duma",
                "contests": {"party": {}, "candidate": {}},
                "nodes": nodes,
            }
        )[0]
        self.assertNotIn("oik_tvd", party)
        self.assertEqual(single["oik_tvd"], "o1")
        self.assertEqual(single["region_tvd"], "r1")

    def test_extracts_exact_official_oik_breadcrumb(self):
        payload = (
            '<a href="region/izbirkom?action=show&amp;tvd=official-oik">'
            'ОИК №219</a><a href="?tvd=other">УИК №1</a>'
        ).encode()
        self.assertEqual(oik_breadcrumbs(payload), [("official-oik", 219)])

    def test_historical_types_discriminate_single_member_report_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_historical_types(
                {
                    "election": "2016-duma",
                    "contests": {
                        "party": {"tic": 233, "uik": 242},
                        "candidate": {"tic": 464, "uik": 463},
                    },
                },
                output,
            )
            types = (output / "protocol" / "types.ts").read_text(encoding="utf-8")
        self.assertIn("reportType: 463;\n  ballot: \"single-member\";", types)
        self.assertIn("district: DistrictRef;", types)
        self.assertIn(
            "export type UikProtocol = PartyUikProtocol | SingleMemberUikProtocol;",
            types,
        )

    def test_nationwide_merges_split_presidential_column_tables(self):
        payload = """<html data-vrn="100100084849062">
        <table class="table-striped">
          <tr><td></td><td>Сумма</td></tr>
          <tr><td>1</td><td>Число избирателей</td><td>30</td></tr>
          <tr><td>2</td><td>Число бюллетеней</td><td>25</td></tr>
          <tr><td>3</td><td>Иванов Иван Иванович</td><td>25 100%</td></tr>
        </table>
        <table class="table-striped">
          <tr><td>УИК №1</td><td>УИК №2</td></tr>
          <tr><td>10</td><td>20</td></tr>
          <tr><td>10</td><td>15</td></tr>
          <tr><td>10 100%</td><td>15 100%</td></tr>
        </table></html>""".encode()
        _, rows = decoded_rows(payload)
        self.assertEqual(rows[0], ["", "", "Сумма", "УИК №1", "УИК №2"])
        self.assertEqual(rows[-1][1], "Иванов Иван Иванович")
        self.assertEqual(rows[-1][3:], ["10 100%", "15 100%"])

    def test_nationwide_hierarchy_accepts_uchastok_uik_label(self):
        match = NATIONWIDE_UIK_RE.fullmatch("Участок  №5031")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "5031")

    def test_nationwide_transpose_detects_variable_accounting_rows(self):
        rows = [
            ["", "", "Сумма", "УИК №1", "УИК №2"],
            ["1", "Число избирателей", "30", "10", "20"],
            ["2", "Число бюллетеней", "25", "10", "15"],
            ["3", "Число строка 3", "25", "10", "15"],
            ["4", "Число строка 4", "25", "10", "15"],
            ["5", "Число строка 5", "25", "10", "15"],
            ["6", "Число строка 6", "25", "10", "15"],
            ["7", "Число строка 7", "25", "10", "15"],
            ["8", "Число строка 8", "25", "10", "15"],
            ["9", "Число строка 9", "25", "10", "15"],
            ["10", "Число строка 10", "25", "10", "15"],
            ["11", "Число строка 11", "25", "10", "15"],
            ["12", "Число строка 12", "25", "10", "15"],
            ["13", "Число строка 13", "25", "10", "15"],
            ["14", "1. Партия", "25", "10 40%", "15 60%"],
        ]
        uiks = {
            1: {"node_id": "u1", "parent_id": "t1"},
            2: {"node_id": "u2", "parent_id": "t1"},
        }
        records, aggregate = transpose(rows, uiks, "party")
        self.assertEqual(len(records[0]["accounting"]), 13)
        self.assertEqual(records[1]["party_votes"], {"1. Партия": 15})
        self.assertEqual(aggregate["votes"], {"1. Партия": 25})

    def test_nationwide_report_kind_uses_election_configuration(self):
        self.assertEqual(
            report_kind(
                {
                    "contests": {
                        "party": {"tic": 431, "uik": 430},
                        "candidate": {"tic": 429, "uik": 428},
                    }
                }
            ),
            {
                431: ("tic", "party"),
                430: ("uik", "party"),
                429: ("tic", "candidate"),
                428: ("uik", "candidate"),
            },
        )

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
