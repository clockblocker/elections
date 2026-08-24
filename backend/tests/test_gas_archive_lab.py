from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.gas_archive_lab.common import extract_tree_nodes  # noqa: E402
from tools.gas_archive_lab.export_uik_results import (  # noqa: E402
    indexed_uiks,
    transpose_table,
)
from tools.gas_archive_lab.probe_matrix import replace_query, variants  # noqa: E402


def test_extract_tree_nodes_preserves_evidence_without_inferring_oik() -> None:
    payload = b"""<base href="http://www.vybory.izbirkom.ru/">
    <script>tvdTreeJson = {"id":10,"text":"Region","href":
    "region/izbirkom?action=show&root=1&tvd=10&vrn=20&region=3&sub_region=3",
    "selected":true,"load_on_demand":false,"isUik":false,"children":[
    {"id":11,"text":"District","href":
    "region/izbirkom?action=show&root=1000005&tvd=11&vrn=20&region=3",
    "load_on_demand":true,"isUik":false,"children":[]}]};</script>"""

    nodes, encoding = extract_tree_nodes(payload)

    assert encoding == "utf-8"
    assert len(nodes) == 2
    assert nodes[1].parent_id == "10"
    assert nodes[1].root == "1000005"
    assert nodes[1].tvd == "11"
    assert nodes[1].text == "District"
    assert not hasattr(nodes[1], "oik_number")


def test_replace_query_handles_null_and_removed_values() -> None:
    url = "http://example.test/path?a=1&pronetvd=0&report_mode=null"
    changed = replace_query(url, {"pronetvd": "null", "report_mode": None})

    assert "pronetvd=null" in changed
    assert "report_mode" not in changed


def test_matrix_includes_live_and_wayback_path_variants() -> None:
    url = "http://www.vybory.izbirkom.ru/region/izbirkom?action=show&region=1&pronetvd=null"
    candidates = variants(url, ["20210928190257"])

    assert any(strategy == "live" for strategy, _ in candidates)
    assert any(strategy.startswith("wayback:") for strategy, _ in candidates)
    assert any("/region/region/izbirkom" in candidate for _, candidate in candidates)
    assert any("www.1.vybory.izbirkom.ru" in candidate for _, candidate in candidates)


def test_transpose_table_joins_uiks_to_exact_parent() -> None:
    tree = {
        "nodes": [
            {"node_id": "u1", "parent_id": "tik", "text": "УИК №118", "url": "u1"},
            {"node_id": "u2", "parent_id": "other", "text": "УИК №119", "url": "u2"},
        ]
    }
    uiks = indexed_uiks(tree, "tik")
    rows = [["", "", "Сумма", "УИК №118"]]
    rows.extend([[str(i), f"metric {i}", "10", str(i)] for i in range(1, 13)])
    rows.append(["13", "Candidate A", "20", "7"])

    records = transpose_table({"rows": rows}, uiks, option_kind="candidate")

    assert records == [
        {
            "uik_number": 118,
            "uik_tvd": "u1",
            "tik_tvd": "tik",
            "official_uik_url": "u1",
            "accounting": {f"metric {i}": i for i in range(1, 13)},
            "candidate_votes": {"Candidate A": 7},
        }
    ]
