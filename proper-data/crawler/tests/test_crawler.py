from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from zipfile import ZipFile

CRAWLER = Path(__file__).parents[1]
sys.path.insert(0, str(CRAWLER))

from common import (
    DEFAULT_REQUEST_HEADERS,
    _redact_proxy_error,
    atomic_write,
    build_request,
    extract_tree_nodes,
)
from decode_script_result import decode_script_tables
from duma2021 import (
    extract_oik_breadcrumbs,
    parse_candidate_registry,
    parse_official_winners,
)
from generate_typescript import generate
from pipeline import classify_result, hierarchy_summary, make_plan, reconcile
from transport import (
    FetchConfig,
    Fetcher,
    GlobalRateLimiter,
    ResponseStore,
    retry_after_seconds,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.value += max(0, seconds)


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        status: int = 200,
        url: str = "http://old.izbirkom.ru/result",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload, self.status, self.code, self.url = payload, status, status, url
        self.headers = Message()
        for key, value in (headers or {"Content-Type": "text/html"}).items():
            self.headers[key] = value

    def read(self) -> bytes:
        return self.payload

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def open(self, request, timeout):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def hierarchy_nodes() -> list[dict]:
    return [
        {
            "node_id": "cec",
            "parent_id": None,
            "text": "CEC",
            "url": "http://www.vybory.izbirkom.ru/region/izbirkom",
            "tvd": "cec",
            "vrn": "100100225883172",
            "region": "0",
            "sub_region": "0",
            "load_on_demand": False,
            "is_uik": False,
        },
        {
            "node_id": "region",
            "parent_id": "cec",
            "text": "Region",
            "url": "http://www.alpha.vybory.izbirkom.ru/region/izbirkom?action=show&tvd=region&vrn=100100225883172&region=1&sub_region=1",
            "tvd": "region",
            "vrn": "100100225883172",
            "region": "1",
            "sub_region": "1",
            "load_on_demand": False,
            "is_uik": False,
        },
        {
            "node_id": "tik",
            "parent_id": "region",
            "text": "TIK",
            "url": "http://www.alpha.vybory.izbirkom.ru/region/izbirkom?action=show&tvd=tik&vrn=100100225883172&region=1&sub_region=1",
            "tvd": "tik",
            "vrn": "100100225883172",
            "region": "1",
            "sub_region": "1",
            "load_on_demand": False,
            "is_uik": False,
        },
        {
            "node_id": "uik",
            "parent_id": "tik",
            "text": "УИК №7",
            "url": "http://www.alpha.vybory.izbirkom.ru/region/izbirkom?action=show&tvd=uik&vrn=100100225883172&region=1&sub_region=1",
            "tvd": "uik",
            "vrn": "100100225883172",
            "region": "1",
            "sub_region": "1",
            "load_on_demand": False,
            "is_uik": True,
        },
    ]


class RateLimiterTests(unittest.TestCase):
    def test_evenly_spaced_at_ten_rps(self):
        clock = FakeClock()
        limiter = GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep)
        starts = [limiter.wait() for _ in range(5)]
        self.assertEqual([round(x, 3) for x in starts], [0, 0.1, 0.2, 0.3, 0.4])

    def test_aggregate_spacing_across_workers(self):
        clock = FakeClock()
        limiter = GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep)
        starts = []

        def work():
            starts.append(limiter.wait())

        workers = [threading.Thread(target=work) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        starts.sort()
        self.assertTrue(all(b - a >= 0.099 for a, b in pairwise(starts)))

    def test_retry_after_delta_and_date(self):
        self.assertEqual(retry_after_seconds("12"), 12)
        now = datetime(2021, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            retry_after_seconds("Fri, 01 Jan 2021 00:00:07 GMT", now=now), 7
        )

    def test_new_cooldown_invalidates_an_already_reserved_slot(self):
        clock = FakeClock()
        limiter = GlobalRateLimiter(10, clock=clock.monotonic)
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


class PersistenceAndRetryTests(unittest.TestCase):
    def test_proxy_credentials_are_redacted_from_transport_errors(self):
        proxy = "socks5h://crawler:topsecret@proxy.test:1080"
        message = _redact_proxy_error(RuntimeError(f"failed via {proxy}"), proxy)
        self.assertNotIn("crawler", message)
        self.assertNotIn("topsecret", message)
        self.assertIn("<configured proxy>", message)

    def test_shared_request_uses_browser_compatible_headers(self):
        request = build_request("http://example.test/")
        self.assertEqual(
            request.get_header("User-agent"), DEFAULT_REQUEST_HEADERS["User-Agent"]
        )
        self.assertIn("ru-RU", request.get_header("Accept-language"))

    def test_atomic_write_leaves_no_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "body"
            atomic_write(path, b"official bytes")
            self.assertEqual(path.read_bytes(), b"official bytes")
            self.assertEqual(list(path.parent.glob("*.part")), [])

    def test_resume_avoids_duplicate_download(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResponseStore(Path(directory))
            clock = FakeClock()
            client = FakeClient([FakeResponse(b"one")])
            fetcher = Fetcher(
                store,
                GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep),
                client=client,
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            first = fetcher.fetch("http://example.test/one")
            second = fetcher.fetch("http://example.test/one")
            self.assertEqual(client.calls, 1)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertTrue(second["cache_hit"])

    def test_response_journal_resumes_and_compacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ResponseStore(root)
            first.save(
                "http://example.test/checkpoint",
                b"official bytes",
                {"status": 200},
            )
            self.assertTrue(first.journal_path.is_file())

            resumed = ResponseStore(root)
            self.assertIsNotNone(
                resumed.verified("http://example.test/checkpoint")
            )
            resumed.flush()

            self.assertTrue(resumed.index_path.is_file())
            self.assertFalse(resumed.journal_path.exists())
            compacted = ResponseStore(root)
            self.assertIsNotNone(
                compacted.verified("http://example.test/checkpoint")
            )

    def test_preserved_error_is_retried_on_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResponseStore(Path(directory))
            clock = FakeClock()
            client = FakeClient([FakeResponse(b"blocked", 403), FakeResponse(b"ok")])
            fetcher = Fetcher(
                store,
                GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep),
                client=client,
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            first = fetcher.fetch("http://example.test/retry-next-run")
            second = fetcher.fetch("http://example.test/retry-next-run")
            self.assertEqual(first["status"], 403)
            self.assertEqual(second["status"], 200)
            self.assertEqual(client.calls, 2)

    def test_retry_and_global_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResponseStore(Path(directory))
            clock = FakeClock()
            client = FakeClient([urllib.error.URLError("down"), FakeResponse(b"ok")])
            limiter = GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep)
            fetcher = Fetcher(
                store,
                limiter,
                FetchConfig(retries=1, backoff_initial=2, backoff_max=2),
                client=client,
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            result = fetcher.fetch("http://example.test/retry")
            self.assertEqual(result["retry_count"], 1)
            self.assertGreaterEqual(clock.value, 1)

    def test_retry_after_is_global_and_every_response_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResponseStore(Path(directory))
            clock = FakeClock()
            client = FakeClient(
                [
                    FakeResponse(b"busy", 503, headers={"Retry-After": "7"}),
                    FakeResponse(b"ok"),
                ]
            )
            limiter = GlobalRateLimiter(10, clock=clock.monotonic, sleep=clock.sleep)
            fetcher = Fetcher(
                store,
                limiter,
                FetchConfig(retries=1),
                client=client,
                clock=clock.monotonic,
                sleep=clock.sleep,
            )
            result = fetcher.fetch("http://example.test/retry-after")
            self.assertGreaterEqual(clock.value, 7)
            self.assertEqual(
                [item["status"] for item in result["observations"]], [503, 200]
            )


class DecodeAndHierarchyTests(unittest.TestCase):
    def test_randomized_function_names_decode_by_behavior(self):
        source = """<html><table class='table table-striped qz'><tr><td class='x'>?</td><td>УИК №7</td></tr><tr><td>1</td><td class='x'>?</td></tr></table><script>
        var zzRandom = function(a,b,c){var x=document.getElementsByClassName(a); x[0].innerHTML = b;};
        var qz = 1; var a = function(){zzRandom('x','42',false);}; a();</script></html>"""
        tables = decode_script_tables(source)
        self.assertEqual(tables[0][0][0], "42")
        self.assertEqual(tables[0][1][1], "42")

    def test_replacement_call_can_span_a_formatting_newline(self):
        source = """<html><table class='table table-striped qz'><tr><td class='x'>obfuscated</td></tr></table><script>
        var zzRandom = function(a,b,c){var x=document.getElementsByClassName(a); x[0].innerHTML = b;};
        var qz = 1; var a = function(){zzRandom('x', '
        39', qz);}; a();</script></html>"""
        self.assertEqual(decode_script_tables(source)[0][0][0], "39")

    def test_css_hidden_decoys_are_not_promoted_as_numbers(self):
        source = """<style>.qq .decoy{position:absolute;left:-999999px}</style>
        <table class='table table-striped qq'><tr><td><span class='decoy'>x</span>12</td></tr></table>"""
        self.assertEqual(decode_script_tables(source)[0][0][0], "12")

    def test_hierarchy_extraction_and_mapping(self):
        payload = b"""<base href="http://old.izbirkom.ru/"><script>tvdTreeJson = {"id":"tik","text":"TIK","children":[{"id":"uik","text":"UIK #7","href":"x?tvd=uik&vrn=100100225883172","isUik":true}]};</script>""".replace(
            b"UIK #", "УИК №".encode()
        )
        nodes, _ = extract_tree_nodes(payload)
        summary = hierarchy_summary([vars(node) for node in nodes])
        self.assertEqual(summary["uiks"], 1)
        self.assertEqual(summary["uik_to_tik"][0]["tik_tvd"], "tik")

    def test_plan_uses_exact_ids_and_all_report_classes(self):
        plan = make_plan(hierarchy_nodes(), direct_uik=True)
        self.assertEqual(plan["estimated_requests"], 4)
        self.assertEqual(
            set(plan["request_classes"]), {"tic-233", "tic-464", "uik-242", "uik-463"}
        )
        self.assertTrue(
            all("old.izbirkom.ru" in row["url"] for row in plan["requests"])
        )

    def test_duma_hierarchy_preserves_region_and_opt_in_oik_ancestors(self):
        nodes = hierarchy_nodes()
        nodes.insert(
            2,
            {
                **nodes[1],
                "node_id": "oik",
                "parent_id": "region",
                "text": "District commission",
                "tvd": "oik",
            },
        )
        next(item for item in nodes if item["node_id"] == "tik")["parent_id"] = "oik"
        relation = hierarchy_summary(nodes, include_oik=True)["uik_to_tik"][0]
        self.assertEqual(relation["region_tvd"], "region")
        self.assertEqual(relation["region_name"], "Region")
        self.assertEqual(relation["oik_tvd"], "oik")
        self.assertNotIn(
            "oik_tvd", hierarchy_summary(nodes)["uik_to_tik"][0]
        )

    def test_exact_oik_breadcrumb_uses_document_base(self):
        payload = b'<base href="http://old.izbirkom.ru/"><a href="region/x?action=show&amp;tvd=1001">' + "ОИК №1".encode() + b"</a>"
        self.assertEqual(
            extract_oik_breadcrumbs(payload, "http://wrong.test/path/page"),
            [
                {
                    "district_number": 1,
                    "oik_tvd": "1001",
                    "url": "http://old.izbirkom.ru/region/x?action=show&tvd=1001",
                }
            ],
        )

    def test_candidate_registry_parses_official_type_220_shape(self):
        payload = (Path(__file__).parent / "fixtures/duma_2021_candidate_220.html").read_bytes()
        parsed = parse_candidate_registry(payload)
        self.assertTrue(parsed["valid_candidate_registry"])
        self.assertEqual(parsed["district_numbers"], [20])
        self.assertEqual(parsed["candidates"][0]["candidate_vibid"], "4934014202239")
        self.assertEqual(parsed["candidates"][0]["nominating_entity"], 'Всероссийская политическая партия "ЕДИНАЯ РОССИЯ"')
        self.assertEqual(parsed["candidates"][0]["registry_election_status"], "избр.")
        self.assertFalse(parsed["candidates"][0]["is_elected"])

    def test_immutable_cec_winner_docx_parses_225_districts(self):
        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rows = "".join(
            f"<w:tr><w:tc><w:p><w:r><w:t>District округ № {number}</w:t></w:r></w:p>"
            f"<w:p><w:r><w:t>Winner {number}</w:t></w:r></w:p></w:tc></w:tr>"
            for number in range(1, 226)
        )
        document = (
            f'<w:document xmlns:w="{namespace}"><w:body><w:tbl>{rows}</w:tbl>'
            "</w:body></w:document>"
        ).encode()
        payload = BytesIO()
        with ZipFile(payload, "w") as archive:
            archive.writestr("word/document.xml", document)
        parsed = parse_official_winners(payload.getvalue())
        self.assertTrue(parsed["valid_winner_registry"])
        self.assertEqual(parsed["winner_count"], 225)
        self.assertEqual(parsed["winners"][2]["full_name"], "Winner 3")


class ValidationAndGenerationTests(unittest.TestCase):
    def test_report_type_classification_uses_contents(self):
        accounting = "".join(
            f"<tr><td>{i}</td><td>{'Число избирателей' if i == 1 else 'Учетная строка'}</td><td>1</td><td>1</td></tr>"
            for i in range(1, 13)
        )

        def page(label):
            return f"<html data-vrn='100100225883172'><table class='table table-striped qq'><tr><td></td><td></td><td>Сумма</td><td>УИК №7</td></tr>{accounting}<tr><td>13</td><td>{label}</td><td>1</td><td>1</td></tr></table><script>var rr=function(a,b,c){{var x=document.getElementsByClassName(a);x[0].innerHTML=b;}}; var qq=1; var a=function(){{}};</script></html>".encode()

        party = classify_result(page('Политическая партия "A"'), 233)
        candidate = classify_result(page("Иванов Иван Иванович"), 464)
        self.assertTrue(party["valid_result"] and party["kind_matches_requested_type"])
        self.assertTrue(
            candidate["valid_result"] and candidate["kind_matches_requested_type"]
        )

    def test_malformed_html_rejected(self):
        result = classify_result(b"<html><h1>Service Unavailable</h1></html>", 233)
        self.assertFalse(result["valid_result"])
        self.assertIn("service unavailable", result["error_signals"])

    def test_reconciliation_reports_difference(self):
        failures = reconcile(
            [{"accounting": {"registered": 10}, "votes": {"A": 4}}],
            {"accounting": {"registered": 11}, "votes": {"A": 4}},
        )
        self.assertEqual(
            failures,
            [
                {
                    "field": "accounting",
                    "label": "registered",
                    "uik_sum": 10,
                    "tik_value": 11,
                    "difference": -1,
                }
            ],
        )

    def test_generation_is_deterministic_and_secret_free(self):
        source = {
            "official_url": "http://old.izbirkom.ru/x",
            "sha256": "a" * 64,
            "retrieved_at": "2021-09-20T00:00:00Z",
            "provenance": "live-official",
        }
        record = {
            "uik_number": 7,
            "uik_tvd": "uik",
            "tik_tvd": "tik",
            "tik_name": "TIK",
            "region": "1",
            "region_code": "1",
            "region_tvd": "region",
            "region_name": "Region",
            "district_number": 20,
            "oik_tvd": "oik",
            "oik_name": "District commission",
            "uik_name": "УИК №7",
            "party_accounting": {"registered": 10},
            "party_votes": {"A": 4},
            "candidate_accounting": {"registered": 10},
            "candidate_votes": {"B": 3},
            "party_source": source,
        }
        data = {
            "records": [record],
            "sources": [
                {
                    "tik_tvd": "tik",
                    "tik_name": "TIK",
                    "region": "1",
                    "party": source,
                    "candidate": source,
                }
            ],
            "districts": [
                {
                    "district_number": 20,
                    "oik_tvd": "oik",
                    "oik_name": "District commission",
                    "region_code": "1",
                    "region_tvd": "region",
                    "region_name": "Region",
                    "winner_candidate_vibid": "candidate",
                    "candidates": [
                        {
                            "candidate_vibid": "candidate",
                            "full_name": "Candidate Name",
                            "nominating_entity": "Nominator",
                            "registration_status": "registered",
                            "registry_election_status": "избр.",
                            "is_elected": True,
                        }
                    ],
                    "source": source,
                    "winner_source": {
                        **source,
                        "resolution": "61/467-8",
                        "resolution_date": "2021-09-24",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generate(data, root)
            before = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.ts")
            }
            generate(data, root)
            after = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.ts")
            }
            self.assertEqual(before, after)
            joined = b"".join(after.values()).lower()
            self.assertNotIn(b"proxy", joined)
            self.assertNotIn(b"password", joined)
            self.assertIn(b"extracted-tic-column", joined)
            self.assertIn(b'"derivation": "direct"', joined)
            self.assertIn(b'"districtnumber": 20', joined)
            self.assertIn("districts/region-1.ts", after)
            self.assertIn(b"UikSingleMemberProtocol", after["protocol/types.ts"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generate(data, root, shard_by_region=True)
            before = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.ts")
            }
            generate(data, root, shard_by_region=True)
            after = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.ts")
            }
            self.assertEqual(before, after)
            self.assertIn("protocol/uik/242/region-1-part-001.ts", after)
            self.assertIn("protocol/tic/464/region-1.ts", after)
            self.assertIn("uik-to-tik/region-1.ts", after)
            self.assertIn("districts/region-1.ts", after)
            self.assertIn(b'"regionTvd": "region"', after["uik-to-tik/region-1.ts"])


if __name__ == "__main__":
    unittest.main()
