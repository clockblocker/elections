from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from house_polling_address_map.cec import CecTreeNode
from house_polling_address_map.legacy import LegacyTreeCrawler, parse_legacy_result
from house_polling_address_map.models import AssignmentStatus
from house_polling_address_map.store import PollingMapStore

NOW = datetime(2018, 3, 18, 12, tzinfo=UTC)


def node(
    node_id: str,
    text: str,
    address_id: str,
    level_id: str,
    *,
    children: bool = True,
    result_token: str | None = None,
) -> CecTreeNode:
    return CecTreeNode(
        node_id=node_id,
        text=text,
        address_id=address_id,
        level_id=level_id,
        result_token=result_token,
        has_children=children,
        raw={
            "id": node_id,
            "text": text,
            "a_attr": {"intid": address_id, "levelid": level_id},
            "children": children,
        },
    )


def tree_body(*nodes: CecTreeNode) -> bytes:
    return json.dumps([item.raw for item in nodes], ensure_ascii=False).encode("windows-1251")


RESULT_HTML = """
<html><head><meta charset="windows-1251"></head><body>
  <p>Участковая избирательная комиссия №1074<br>
     Номер Территориальной избирательной комиссии: 010</p>
  <p>Адрес помещения УИК: 394088, город Воронеж, улица Лизюкова, дом 52а</p>
  <p>Телефон УИК: 8-(473)-202-27-20</p>
  <p>Адрес помещения для голосования: 394088, город Воронеж, улица Лизюкова,
     дом 52а, школа №94</p>
  <p>Телефон помещения для голосования: 8-(473)-202-27-21</p>
</body></html>
""".encode("windows-1251")


class FakeTree:
    def __init__(self, *, fail_result_once: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail_result_once = fail_result_once
        self._failed = False
        self.region = node("region-36", "Воронежская область", "a-region", "2")
        self.city = node("city", "город Воронеж", "a-city", "4")
        self.street = node("street", "улица Лизюкова", "a-street", "7")
        self.house = node("house", "52а", "a-house", "8")

    def roots(self):
        self.calls.append(("roots", None))
        body = tree_body(self.region)
        return "https://example.test/services/lk_tree/?first=1", 200, body, (self.region,)

    def children(self, node_id: str):
        self.calls.append(("children", node_id))
        children = {
            "region-36": (self.city,),
            "city": (self.street,),
            "street": (self.house,),
            "house": (),
        }[node_id]
        body = tree_body(*children)
        return f"https://example.test/services/lk_tree/?id={node_id}", 200, body, children

    def result(self, address_id: str):
        self.calls.append(("result", address_id))
        if self.fail_result_once and not self._failed:
            self._failed = True
            raise RuntimeError("temporary archive failure")
        return (
            f"https://example.test/services/lk_address/{address_id}?do=result",
            200,
            RESULT_HTML,
        )


class LegacyResultParserTest(unittest.TestCase):
    def test_parses_only_labeled_result_fields_and_prefers_voting_phone(self) -> None:
        result = parse_legacy_result(RESULT_HTML)

        self.assertEqual(AssignmentStatus.RESOLVED, result.status)
        self.assertEqual(1074, result.uik_number)
        self.assertEqual(
            "394088, город Воронеж, улица Лизюкова, дом 52а",
            result.commission_address,
        )
        self.assertTrue((result.polling_place_address or "").endswith("школа №94"))
        self.assertEqual("8-(473)-202-27-21", result.telephone)

    def test_marks_conflicting_uik_numbers_ambiguous(self) -> None:
        html = """
        <p>Участковая избирательная комиссия №7</p>
        <p>Участковая избирательная комиссия №8</p>
        <p>Адрес помещения для голосования: город Москва, улица Пушкина, 1</p>
        """.encode()

        result = parse_legacy_result(html)

        self.assertEqual(AssignmentStatus.AMBIGUOUS, result.status)
        self.assertIsNone(result.uik_number)

    def test_marks_incomplete_or_unlabeled_pages_failed(self) -> None:
        result = parse_legacy_result(
            "<p>Участковая избирательная комиссия №44</p><p>Москва, дом 10</p>".encode()
        )
        self.assertEqual(AssignmentStatus.FAILED, result.status)
        self.assertIn("polling-place address", result.note or "")


class LegacyTreeCrawlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.store = PollingMapStore(root / "map.sqlite3", root / "raw")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def crawler(self, tree: FakeTree) -> LegacyTreeCrawler:
        return LegacyTreeCrawler(
            tree,  # type: ignore[arg-type]
            self.store,
            crawl_id="fixture-2018",
            delay_seconds=0,
            now=lambda: NOW,
        )

    def test_exhaustively_walks_empty_terminal_children_and_records_mapping(self) -> None:
        tree = FakeTree()

        summary = self.crawler(tree).run()

        self.assertEqual(6, summary.requests)
        self.assertEqual(0, summary.pending_jobs)
        self.assertEqual(1, summary.resolved)
        building = self.store._connection.execute("SELECT * FROM buildings").fetchone()
        self.assertEqual("cec-legacy:a-house", building["address_id"])
        self.assertEqual("36", building["region_code"])
        self.assertEqual("город Воронеж", building["locality"])
        self.assertEqual("улица Лизюкова", building["street"])
        self.assertEqual("52а", building["house"])
        station = self.store._connection.execute("SELECT * FROM polling_stations").fetchone()
        self.assertEqual("36:1074", station["station_id"])
        evidence = self.store._connection.execute("SELECT * FROM assignments").fetchone()
        self.assertEqual("resolved", evidence["status"])
        self.assertEqual(
            6, self.store._connection.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
        )

    def test_request_limit_and_restart_resume_without_refetching_completed_jobs(self) -> None:
        tree = FakeTree()
        first = self.crawler(tree).run(request_limit=2)
        self.assertEqual(2, first.requests)
        self.assertGreater(first.pending_jobs, 0)

        second = self.crawler(tree).run()

        self.assertEqual(4, second.requests)
        self.assertEqual(0, second.pending_jobs)
        self.assertEqual(1, tree.calls.count(("roots", None)))
        self.assertEqual(1, tree.calls.count(("children", "region-36")))
        self.assertEqual(1, self.store.coverage("36").resolved)

    def test_transient_result_failure_remains_pending_then_succeeds_on_restart(self) -> None:
        tree = FakeTree(fail_result_once=True)
        first = self.crawler(tree).run()
        self.assertEqual(1, first.pending_jobs)
        self.assertEqual(
            0, self.store._connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        )

        second = self.crawler(tree).run()

        self.assertEqual(1, second.requests)
        self.assertEqual(0, second.pending_jobs)
        self.assertEqual(1, self.store.coverage("36").resolved)
        self.assertEqual(1, tree.calls.count(("roots", None)))


if __name__ == "__main__":
    unittest.main()
