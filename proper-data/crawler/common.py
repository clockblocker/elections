from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

TREE_MARKER = re.compile(r"\btvdTreeJson\s*=\s*(?=\{)")


@dataclass(frozen=True)
class TreeNode:
    node_id: str | None
    parent_id: str | None
    text: str
    href: str | None
    url: str | None
    root: str | None
    tvd: str | None
    vrn: str | None
    region: str | None
    sub_region: str | None
    selected: bool
    load_on_demand: bool
    is_uik: bool


class BaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "base":
            return
        href = dict(attrs).get("href")
        if href:
            self.base_href = href


def decode_text(payload: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "windows-1251"):
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace"), "utf-8-replacement"


def extract_tree_nodes(payload: bytes, source_url: str = "") -> tuple[list[TreeNode], str]:
    html, encoding = decode_text(payload)
    base_parser = BaseParser()
    base_parser.feed(html)
    base_url = urllib.parse.urljoin(source_url, base_parser.base_href or "")
    decoder = json.JSONDecoder()
    nodes: list[TreeNode] = []

    def visit(raw: Any, parent_id: str | None) -> None:
        if not isinstance(raw, dict):
            return
        node_id = str(raw["id"]) if raw.get("id") is not None else None
        href = raw.get("href") if isinstance(raw.get("href"), str) else None
        url = urllib.parse.urljoin(base_url, href) if href else None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query)
        value = lambda name: query.get(name, [None])[0]
        nodes.append(
            TreeNode(
                node_id=node_id,
                parent_id=parent_id,
                text=str(raw.get("text") or ""),
                href=href,
                url=url,
                root=value("root"),
                tvd=value("tvd"),
                vrn=value("vrn"),
                region=value("region"),
                sub_region=value("sub_region"),
                selected=bool(raw.get("selected")),
                load_on_demand=bool(raw.get("load_on_demand")),
                is_uik=bool(raw.get("isUik")),
            )
        )
        for child in raw.get("children", []):
            visit(child, node_id)

    for match in TREE_MARKER.finditer(html):
        try:
            tree, _ = decoder.raw_decode(html[match.end() :])
        except json.JSONDecodeError:
            continue
        visit(tree, None)
    return nodes, encoding


def node_dicts(nodes: list[TreeNode]) -> list[dict[str, Any]]:
    return [asdict(node) for node in nodes]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def safe_name(url: str, suffix: str = ".bin") -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + suffix


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler())
