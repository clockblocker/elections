from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Self

import requests

TREE_MARKER = re.compile(r"\btvdTreeJson\s*=\s*(?=\{)")
ROOT_ENV = Path(__file__).parents[2] / ".env"
DEFAULT_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


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


def extract_tree_nodes(
    payload: bytes, source_url: str = "", parent_id: str | None = None
) -> tuple[list[TreeNode], str]:
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
        visit(tree, parent_id)
    if not nodes:
        stripped = html.strip()
        if stripped.startswith(("{", "[")):
            try:
                tree = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                values = tree if isinstance(tree, list) else [tree]
                for value in values:
                    visit(value, parent_id)
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
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def atomic_write(path: Path, payload: bytes) -> None:
    """Durably publish bytes without exposing a partial response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
) -> urllib.request.Request:
    """Build a request with the headers required by the official CIK/GAS sites."""
    return urllib.request.Request(
        url,
        data=data,
        headers={**DEFAULT_REQUEST_HEADERS, **(headers or {})},
        method=method,
    )


def _dotenv_value(name: str, path: Path = ROOT_ENV) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


def configured_proxy_url() -> str | None:
    """Return the crawler-only proxy without publishing it process-wide."""
    return os.environ.get("PROPER_DATA_PROXY_URL") or _dotenv_value(
        "PROPER_DATA_PROXY_URL"
    )


def _redact_proxy_error(error: Exception, proxy_url: str | None) -> str:
    message = f"{type(error).__name__}: {error}"
    if not proxy_url:
        return message
    message = message.replace(proxy_url, "<configured proxy>")
    parts = urllib.parse.urlsplit(proxy_url)
    for secret in (parts.username, parts.password):
        if secret:
            message = message.replace(secret, "***")
            message = message.replace(urllib.parse.quote(secret, safe=""), "***")
    return message


class _Response:
    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.status = self.code = response.status_code
        self.headers = response.headers

    def read(self) -> bytes:
        return self._response.content

    def geturl(self) -> str:
        return self._response.url

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> bool:
        self.close()
        return False


class RequestsOpener:
    """Small urllib-compatible client with SOCKS support through requests."""

    def __init__(self, proxy_url: str | None = None) -> None:
        self.proxy_url = proxy_url if proxy_url is not None else configured_proxy_url()

    def open(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _Response:
        headers = {**DEFAULT_REQUEST_HEADERS, **dict(request.header_items())}
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        try:
            response = requests.request(
                request.get_method(),
                request.full_url,
                headers=headers,
                data=request.data,
                proxies=proxies,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as error:
            raise urllib.error.URLError(
                _redact_proxy_error(error, self.proxy_url)
            ) from error
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                response.url,
                response.status_code,
                response.reason,
                response.headers,
                io.BytesIO(response.content),
            )
        return _Response(response)


def opener() -> RequestsOpener:
    return RequestsOpener()
