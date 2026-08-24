from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.gas_archive_lab.common import extract_tree_nodes  # noqa: E402
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
