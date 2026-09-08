from __future__ import annotations

import json
import unittest

from house_polling_address_map.cec import (
    CecLegacyTree,
    CecLookup,
    CecProtocol,
    CecResponseError,
)


class CecLookupTest(unittest.TestCase):
    def test_extracts_resolved_and_parent_suggestions_from_wrapped_json(self) -> None:
        payload = {
            "suggestions": [
                {
                    "value": "Смоленская область, город Смоленск, Ленина улица",
                    "data": {"intid": "street-1", "region_id": 69, "commission_id": None},
                },
                {
                    "value": "Смоленская область, город Смоленск, Ленина улица, 12А",
                    "data": {"intid": "house-1", "region_id": 69, "commission_id": 1124357},
                },
            ]
        }

        def transport(request, timeout):
            self.assertIn("query=", request.full_url)
            self.assertEqual(timeout, 5)
            return 200, json.dumps(payload, ensure_ascii=False).encode()

        lookup = CecLookup(
            CecProtocol("https://example.invalid/suggest"), timeout=5, transport=transport
        )
        response = lookup.suggest("Смоленск Ленина")

        self.assertEqual(2, len(response.suggestions))
        self.assertFalse(response.suggestions[0].is_resolved)
        self.assertEqual("1124357", response.suggestions[1].commission_id)
        self.assertEqual("house-1", response.suggestions[1].address_id)
        self.assertEqual("69", response.suggestions[1].region_code)

    def test_rejects_non_json_response(self) -> None:
        lookup = CecLookup(
            CecProtocol("https://example.invalid/suggest"),
            transport=lambda request, timeout: (200, b"<html>blocked</html>"),
        )
        with self.assertRaises(CecResponseError):
            lookup.suggest("Москва Тверская 1")

    def test_committee_endpoint_is_explicitly_configured(self) -> None:
        seen = []

        def transport(request, timeout):
            seen.append(request.full_url)
            return 200, b"{}"

        lookup = CecLookup(
            CecProtocol(
                "https://example.invalid/suggest",
                committee_url=(
                    "https://example.invalid/committee/subjcode/{region_code}/"
                    "num/{committee_number}"
                ),
            ),
            transport=transport,
        )
        lookup.committee("01", "2")
        self.assertEqual(["https://example.invalid/committee/subjcode/01/num/2"], seen)

    def test_walks_legacy_windows_1251_tree_and_fetches_result(self) -> None:
        requests = []
        payload = [
            {
                "id": "volatile-7",
                "text": "Улица Ленина",
                "a_attr": {"intid": "stable-8", "levelid": 7, "ret": "1"},
                "children": True,
            }
        ]

        def transport(request, timeout):
            requests.append(request.full_url)
            if "lk_tree" in request.full_url:
                return 200, json.dumps(payload, ensure_ascii=False).encode("windows-1251")
            return 200, "Участковая избирательная комиссия №2".encode("windows-1251")

        tree = CecLegacyTree("https://example.invalid", transport=transport)
        _, _, raw, roots = tree.roots()
        self.assertIn(b"volatile-7", raw)
        self.assertEqual("stable-8", roots[0].address_id)
        self.assertEqual("7", roots[0].level_id)
        self.assertTrue(roots[0].has_children)

        url, status, result = tree.result(roots[0].address_id or "")
        self.assertEqual(200, status)
        self.assertTrue(url.endswith("/services/lk_address/stable-8?do=result"))
        self.assertIn("комиссия".encode("windows-1251"), result)

    def test_legacy_tree_preserves_special_result_token(self) -> None:
        seen = []

        def transport(request, timeout):
            seen.append(request.full_url)
            return 200, b"[]"

        tree = CecLegacyTree("https://example.invalid", transport=transport)
        tree.children("moscow-node", result_token="0")
        self.assertIn("id=moscow-node", seen[0])
        self.assertIn("ret=0", seen[0])

    def test_legacy_root_unwraps_embedded_regions(self) -> None:
        region = {
            "id": "region-54",
            "text": "Новосибирская область",
            "a_attr": {"intid": "address-region-54", "levelid": 2},
            "children": True,
        }
        payload = [
            {
                "id": "russia",
                "text": "Россия",
                "a_attr": {"intid": "", "levelid": 1},
                "children": [region],
            }
        ]
        tree = CecLegacyTree(
            "https://example.invalid",
            transport=lambda request, timeout: (
                200,
                json.dumps(payload, ensure_ascii=False).encode("windows-1251"),
            ),
        )

        _, _, _, roots = tree.roots()

        self.assertEqual(1, len(roots))
        self.assertEqual("region-54", roots[0].node_id)
        self.assertEqual("Новосибирская область", roots[0].text)

    def test_path_protocol_resolves_terminal_address_to_polling_place(self) -> None:
        suggestion = [{"id": "addr/1", "name": "Адыгейск, Ленина, 16", "leaf": True}]
        committee = {
            "vrn": "4014001117979",
            "name": "Участковая избирательная комиссия №2",
            "subjCode": "01",
            "address": {"address": "адрес комиссии", "phone": "1"},
            "votingAddress": {"address": "адрес голосования", "phone": "2"},
        }

        def transport(request, timeout):
            if "/search/" in request.full_url:
                return 200, json.dumps(suggestion, ensure_ascii=False).encode()
            return 200, json.dumps(committee, ensure_ascii=False).encode()

        lookup = CecLookup(
            CecProtocol(
                "https://example.invalid/address/search/{query}/",
                resolve_url="https://example.invalid/committee/address/{address_id}",
            ),
            transport=transport,
        )
        response = lookup.suggest("Адыгейск Ленина 16")
        self.assertIn("%D0%90", response.url)
        self.assertTrue(response.suggestions[0].leaf)
        url, _, _, result = lookup.resolve(response.suggestions[0].address_id or "")
        self.assertTrue(url.endswith("addr%2F1"))
        self.assertEqual(2, result.uik_number)
        self.assertEqual("адрес комиссии", result.commission_address)
        self.assertEqual("адрес голосования", result.polling_place_address)
        self.assertEqual("2", result.telephone)


if __name__ == "__main__":
    unittest.main()
