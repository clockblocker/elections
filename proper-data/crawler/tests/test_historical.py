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
    _district_value,
    validate_registry_catalogs,
    winner_registry_source,
)
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
    candidate_label_matches,
    decoded_rows,
    enriched_relations,
    finalize_result_classification,
    oik_breadcrumbs,
    report_kind,
    transpose,
)
from pipeline import UIK_RE as NATIONWIDE_UIK_RE
from registries import (
    normalized_choice_name,
    parse_candidate_registry,
    parse_legacy_party_detail,
    parse_party_registry,
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
    def test_regional_candidate_registry_uses_compact_status_columns(self):
        payload = """
        <html><body>
          <a href="?vrn=27720001368289">Election</a>
          <table><tr>
            <td>1</td>
            <td><a href="?type=341&amp;vibid=123">Иванов Иван Иванович</a></td>
            <td>01.02.1970</td><td>Самовыдвижение</td>
            <td>выдвинут</td><td>зарегистрирован</td><td>избран</td>
          </tr></table>
        </body></html>
        """.encode()
        parsed = parse_candidate_registry(
            payload,
            "http://old.izbirkom.ru/region/izbirkom?vrn=27720001368289&type=221",
            election_vrn="27720001368289",
            scope="election",
        )
        self.assertTrue(parsed["valid_candidate_registry"])
        self.assertEqual(parsed["candidate_count"], 1)
        self.assertEqual(parsed["candidates"][0]["registration_status"], "зарегистрирован")
        self.assertTrue(parsed["candidates"][0]["is_elected"])

    def test_district_catalog_requires_and_serializes_winner_proof(self):
        winner_source = {
            "official_url": "http://old.izbirkom.ru/result?type=223",
            "sha256": "b" * 64,
            "report_type": 223,
            "retrieved_at": "2026-08-28T00:00:00Z",
            "final_url": "http://old.izbirkom.ru/result?type=223",
            "provenance": "live-official",
        }
        with self.assertRaisesRegex(ValueError, "winner derivation"):
            winner_registry_source(
                winner_source,
                allowed_report_types={223},
                require_tik_oik_proof=True,
            )
        winner_source.update(
            {
                "winner_derivation": (
                    "summed-official-tik-results-validated-against-oik-total"
                ),
                "oik_unique_highest_vote_total": 42,
                "summed_tik_winner_vote_total": 42,
            }
        )
        dataset = {
            "election": "2003-duma",
            "registry_gates": {
                "allowed_anomalies": [
                    {
                        "allowed": True,
                        "kind": "obfuscated-oik-winner-label",
                        "district_number": 3,
                        "raw_label": "9.",
                        "winner_candidate_key": "gas:candidate-vibid:winner",
                    }
                ]
            },
        }
        registry_source = {
            "official_url": "http://old.izbirkom.ru/candidates?type=220",
            "sha256": "a" * 64,
            "report_type": 220,
            "retrieved_at": "2026-08-28T00:00:00Z",
            "final_url": "http://old.izbirkom.ru/candidates?type=220",
            "provenance": "live-official",
        }
        district = {
            "district_number": 3,
            "oik_tvd": "oik-3",
            "oik_name": "ОИК №3",
            "region_code": "1",
            "region_tvd": "region-1",
            "region_name": "Регион",
            "winner_candidate_vibid": "winner",
            "candidates": [
                {
                    "candidate_vibid": "winner",
                    "candidate_key": "gas:candidate-vibid:winner",
                    "full_name": "Иванов Иван Иванович",
                    "nominating_entity": "Самовыдвижение",
                    "registration_status": "зарегистрирован",
                    "is_elected": True,
                }
            ],
            "source": registry_source,
            "winner_source": winner_source,
        }
        generated = _district_value(dataset, district, None)
        self.assertEqual(
            generated["obfuscatedWinnerLabelAnomalies"],
            [
                {
                    "districtNumber": 3,
                    "rawLabel": "9.",
                    "winnerCandidateKey": "gas:candidate-vibid:winner",
                    "winnerCandidateVibid": "winner",
                }
            ],
        )
        self.assertEqual(
            generated["source"]["winnerSource"]["oikUniqueHighestVoteTotal"],
            42,
        )

    def test_same_type_tik_direct_protocol_is_classified_as_official_aggregate(self):
        classification = {
            "valid_result": True,
            "level": "uik-direct-protocol",
        }
        same_type = finalize_result_classification(
            classification,
            request_class="tic-226",
            report_type=226,
            contest_types={"tic": 226, "uik": 226},
            exact_report_type=True,
        )
        self.assertEqual(same_type["level"], "tik-direct-protocol")
        self.assertTrue(same_type["kind_matches_requested_type"])

        distinct_types = finalize_result_classification(
            classification,
            request_class="tic-227",
            report_type=227,
            contest_types={"tic": 227, "uik": 226},
            exact_report_type=True,
        )
        self.assertEqual(distinct_types["level"], "uik-direct-protocol")
        self.assertFalse(distinct_types["kind_matches_requested_type"])

    def test_candidate_name_collision_requires_exact_birth_date(self):
        candidate = {
            "full_name": "Николаев Олег Алексеевич",
            "birth_date": "01.12.1953",
        }
        self.assertTrue(
            candidate_label_matches("7. Николаев Олег Алексеевич 01/12/53", candidate)
        )
        self.assertFalse(
            candidate_label_matches("Николаев Олег Алексеевич 24/11/61", candidate)
        )
        self.assertFalse(
            candidate_label_matches(
                "Николаев Олег Алексеевич лишний текст 01/12/53", candidate
            )
        )

    def test_2004_candidate_catalog_accepts_only_the_official_special_vote_key(self):
        registry_source = {
            "official_url": "http://old.izbirkom.ru/candidates?type=221",
            "sha256": "a" * 64,
            "report_type": 221,
            "retrieved_at": "2026-08-28T00:00:00Z",
            "final_url": "http://old.izbirkom.ru/candidates?type=221",
            "provenance": "live-official",
        }
        winner_source = {
            "official_url": "http://old.izbirkom.ru/result?type=226",
            "sha256": "b" * 64,
            "report_type": 226,
            "retrieved_at": "2026-08-28T00:00:00Z",
            "final_url": "http://old.izbirkom.ru/result?type=226",
            "provenance": "live-official",
        }
        votes = {
            "gas:candidate-vibid:winner": 10,
            "special:against-all": 1,
        }
        partial_tiks = [
            "206200075649",
            "228200074900",
            "241200070733",
            "241200070735",
            "243200083386",
            "251200077705",
            "265200075594",
            "266200078108",
            "266200078117",
            "266200084492",
            "266200084493",
            "2772000100984",
            "289200072363",
            "784700068324",
        ]
        dataset = {
            "election": "2004-president",
            "contests": {"candidate": {"tic": 227, "uik": 226}},
            "relations": [],
            "records": [{"candidate_votes": votes}],
            "tik_protocols": [
                {
                    "tik_tvd": tik_tvd,
                    "candidate": {"votes": {"gas:candidate-vibid:winner": 10}},
                }
                for tik_tvd in partial_tiks
            ],
            "candidate_catalog": {
                "winner_candidate_vibid": "winner",
                "candidates": [
                    {
                        "candidate_vibid": "winner",
                        "candidate_key": "gas:candidate-vibid:winner",
                        "full_name": "Иванов Иван Иванович",
                        "nominating_entity": "Самовыдвижение",
                        "registration_status": "зарегистрирован",
                        "is_elected": True,
                    }
                ],
                "source": registry_source,
                "winner_source": winner_source,
            },
            "registry_gates": {
                "passed": True,
                "sources_complete": True,
                "registry_counts_complete": True,
                "every_result_choice_matched_to_official_identity": True,
                "identity_key_formulas_valid": True,
                "uik_and_tik_vote_key_sets_valid": True,
                "regular_and_special_winner_keys_exclusive": True,
                "allowed_source_anomalies_exact": True,
                "literal_elected_and_unique_highest_winner_agree": True,
                "allowed_anomalies": [
                    {
                        "allowed": True,
                        "kind": "partial-official-tik-candidate-map",
                        "election": "2004-president",
                        "tik_tvd": tik_tvd,
                        "present_vote_keys": ["gas:candidate-vibid:winner"],
                        "missing_vote_keys": ["special:against-all"],
                    }
                    for tik_tvd in partial_tiks
                ],
                "errors": {},
            },
        }
        validate_registry_catalogs(dataset)
        dataset["candidate_catalog"]["winner_source"]["report_type"] = 223
        with self.assertRaisesRegex(ValueError, "unexpected winner report type"):
            validate_registry_catalogs(dataset)

    def test_modern_presidential_candidate_registry_preserves_vibid_and_status(self):
        payload = """<html data-vrn="100100339410030"><table id="candidates-221-1">
        <tr><td>1</td><td><a href="?type=341&amp;vibid=winner">Иванов Иван Иванович</a></td>
        <td>01.01.1970</td><td>Самовыдвижение</td><td>01.01.2024</td>
        <td>02.01.2024</td><td>выдвинут</td><td>зарегистрирован</td><td>избран</td></tr>
        </table></html>""".encode("windows-1251")
        parsed = parse_candidate_registry(
            payload,
            "http://old.izbirkom.ru/region/izbirkom?vrn=100100339410030",
            election_vrn="100100339410030",
            scope="election",
        )
        self.assertTrue(parsed["valid_candidate_registry"])
        self.assertEqual(parsed["candidate_count"], 1)
        self.assertEqual(parsed["candidates"][0]["candidate_vibid"], "winner")
        self.assertEqual(parsed["candidates"][0]["birth_date"], "01.01.1970")
        self.assertTrue(parsed["candidates"][0]["is_elected"])

    def test_legacy_district_registry_keeps_official_history_rows(self):
        payload = """<html data-vrn="100100095619"><table><tr>
        <td><a href="?type=341&amp;vibid=c1">Петров Петр Петрович</a></td>
        <td>Самовыдвижение</td><td>Тестовый/17</td><td>01.10.2003</td>
        <td></td><td>4/2 зарегистрированный кандидат</td><td></td><td></td><td></td><td>избр.</td>
        </tr></table></html>""".encode("windows-1251")
        parsed = parse_candidate_registry(
            payload,
            "http://old.izbirkom.ru/region/izbirkom?vrn=100100095619",
            election_vrn="100100095619",
            scope="district",
        )
        self.assertEqual(parsed["district_numbers"], [17])
        self.assertEqual(parsed["candidates"][0]["registration_status"], "4/2 зарегистрированный кандидат")

    def test_party_registry_uses_vrnio_and_legacy_vibid_namespaces(self):
        modern = """<html data-vrn="100100028713299"><table id="politparty2"><tr>
        <td>1</td><td><form><input name="vrnio" value="list-1"><a href="#">\"Партия\"</a></form></td>
        <td>01.01.2011</td><td>1/1</td><td>02.01.2011</td><td>2/2</td><td></td>
        </tr></table></html>""".encode("windows-1251")
        parsed = parse_party_registry(
            modern,
            "http://old.izbirkom.ru/region/izbirkom?vrn=100100028713299",
            election_vrn="100100028713299",
        )
        self.assertEqual(parsed["parties"][0]["party_list_vrnio"], "list-1")
        self.assertEqual(parsed["parties"][0]["identity_kind"], "vrnio")
        legacy = """<html data-vrn="100100095619"><table><tr>
        <td><a href="?type=303&amp;vibid=association-1">Партия</a></td>
        <td>избирательное объединение</td><td>01.01.2002</td><td>05001</td>
        </tr></table></html>""".encode("windows-1251")
        parsed = parse_party_registry(
            legacy,
            "http://old.izbirkom.ru/region/izbirkom?vrn=100100095619",
            election_vrn="100100095619",
        )
        self.assertEqual(parsed["parties"][0]["party_list_vrnio"], "association-1")
        self.assertEqual(parsed["parties"][0]["identity_kind"], "vibid")

    def test_legacy_party_detail_and_choice_normalization(self):
        payload = """<html data-vrn="100100095619"><table>
        <tr><td>Наименование</td><td>\"Партия\"</td></tr>
        <tr><td>Вид</td><td>избирательное объединение</td></tr>
        <tr><td>Номер жеребьевки</td><td>3</td></tr>
        <tr><td>Количество мандатов по партийному списку</td><td>5</td></tr>
        <tr><td>Количество голосов 'За'</td><td>100</td></tr>
        <tr><td>Процент голосов 'За'</td><td>1.25</td></tr>
        <tr><td>Объединение</td><td><a href="?type=321&amp;vibid=list-1">Партия</a></td></tr>
        </table></html>""".encode("windows-1251")
        parsed = parse_legacy_party_detail(
            payload,
            "http://old.izbirkom.ru/region/izbirkom?vrn=100100095619&vibid=association-1&type=303",
            election_vrn="100100095619",
        )
        self.assertTrue(parsed["valid_party_detail"])
        self.assertEqual(parsed["list_vibid"], "list-1")
        self.assertEqual(parsed["draw_number"], 3)
        self.assertEqual(parsed["mandates"], 5)
        self.assertEqual(
            normalized_choice_name('7. Политическая партия ЛДПР – Россия'),
            normalized_choice_name('"Политическая партия ЛДПР - Россия"'),
        )

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
