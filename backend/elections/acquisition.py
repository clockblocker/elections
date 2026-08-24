from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO

from elections.sources import SourceError, sha256_file

MANIFEST_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
PRESERVED_STATUSES = {"preserved", "redirected"}
GAP_STATUSES = {"unavailable", "malformed", "redirected", "inconsistent"}
BLOCKING_GAP_STATUSES = {"unavailable", "malformed", "inconsistent"}

_OIK_RE = re.compile(r"(?:ОИК|одномандат\w*\s+избирательн\w*\s+округ\w*)\s*№?\s*(\d+)", re.I)
_TIK_RE = re.compile(r"\bТИК\b[^\n<]{0,160}", re.I)
_UIK_RE = re.compile(r"\bУИК\s*№?\s*(\d+)\b", re.I)
_FONT_RE = re.compile(r'url\("\./([^"]+\.ttf)"\)')


@dataclass(frozen=True)
class Scope:
    oik_number: int | None = None
    region_name: str | None = None
    oik_name: str | None = None
    tik_name: str | None = None
    uik_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class PendingPage:
    url: str
    scope: Scope = field(default_factory=Scope)
    relation: str = "index"


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    hierarchy: bool = False


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._url: str | None = None
        self._text: list[str] = []
        self.all_text: list[str] = []
        self.base_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.casefold() == "base" and values.get("href"):
            self.base_url = values["href"]
        elif tag.casefold() == "a" and values.get("href"):
            self._url = values["href"]
            self._text = []
        elif tag.casefold() == "option" and values.get("value"):
            self._url = values["value"]
            self._text = []

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._url is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._url is not None and tag.casefold() in {"a", "option"}:
            self.links.append(Link(self._url, " ".join("".join(self._text).split())))
            self._url = None
            self._text = []


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SourceError(f"{description} {path} must contain a JSON object")
    return document


def load_acquisition_plan(path: Path) -> dict[str, Any]:
    document = _read_json(path, "acquisition plan")
    if document.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SourceError(f"unsupported acquisition plan version in {path}")
    snapshot_key = document.get("snapshot_key")
    if not isinstance(snapshot_key, str) or not snapshot_key:
        raise SourceError(f"acquisition plan {path} has no snapshot_key")

    expected = document.get("expected_oiks")
    if not isinstance(expected, list) or not expected:
        raise SourceError(f"acquisition plan {path} must enumerate expected_oiks")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in expected:
        if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
            raise SourceError("each expected OIK must be an object with an integer number")
        number = raw["number"]
        if number < 1 or number in seen:
            raise SourceError(f"invalid or duplicate expected OIK number: {number}")
        seen.add(number)
        normalized.append({"number": number, **{k: v for k, v in raw.items() if k != "number"}})
    expected_count = int(document.get("expected_oik_count", len(normalized)))
    if len(normalized) != expected_count:
        raise SourceError(
            f"acquisition plan expected {expected_count} OIKs but enumerates {len(normalized)}"
        )
    missing_regions = [item["number"] for item in normalized if not item.get("region_name")]
    if missing_regions:
        raise SourceError(
            "acquisition plan must provide region_name for every OIK; missing "
            + ", ".join(map(str, missing_regions))
        )

    seeds = document.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise SourceError(f"acquisition plan {path} must contain at least one seed")
    for seed in seeds:
        if not isinstance(seed, dict) or not isinstance(seed.get("url"), str):
            raise SourceError("each acquisition seed must contain a URL")
    document["expected_oiks"] = sorted(normalized, key=lambda item: item["number"])
    return document


def _canonical_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path, query, "")
    )


def _decoded_html(payload: bytes, media_type: str) -> str | None:
    if "html" not in media_type.casefold():
        return None
    for encoding in ("utf-8", "windows-1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


def _tree_links(html: str, base_url: str) -> list[Link]:
    """Extract links rendered client-side from the CEC's embedded hierarchy JSON."""
    marker = re.compile(r"\btvdTreeJson\s*=\s*(?=\{)")
    decoder = json.JSONDecoder()
    links: list[Link] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            href = node.get("href")
            if isinstance(href, str) and href:
                links.append(
                    Link(
                        urllib.parse.urljoin(base_url, href),
                        str(node.get("text") or ""),
                        hierarchy=True,
                    )
                )
            for child in node.get("children", []):
                visit(child)

    for match in marker.finditer(html):
        try:
            tree, _ = decoder.raw_decode(html[match.end() :])
        except json.JSONDecodeError:
            continue
        visit(tree)
    return links


def _parse_links(payload: bytes, media_type: str, base_url: str) -> tuple[list[Link], str]:
    html = _decoded_html(payload, media_type)
    if html is None:
        raise SourceError(f"unexpected media type {media_type!r} from {base_url}")
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception as exc:
        raise SourceError(f"malformed HTML from {base_url}: {exc}") from exc
    document_base = urllib.parse.urljoin(base_url, parser.base_url or "")
    links = [
        Link(urllib.parse.urljoin(document_base, item.url), item.text) for item in parser.links
    ]
    links.extend(_tree_links(html, document_base))
    return links, " ".join(" ".join(parser.all_text).split())


def _scope_from_link(
    parent: Scope, link: Link, expected: dict[int, dict[str, Any]]
) -> Scope | None:
    oik_match = _OIK_RE.search(link.text)
    if oik_match:
        number = int(oik_match.group(1))
        return Scope(
            oik_number=number,
            region_name=expected.get(number, {}).get("region_name"),
            oik_name=link.text,
        )
    tik_match = _TIK_RE.search(link.text)
    if tik_match and parent.oik_number is not None:
        return Scope(
            oik_number=parent.oik_number,
            region_name=parent.region_name,
            oik_name=parent.oik_name,
            tik_name=link.text,
        )
    uik_matches = tuple(sorted({int(value) for value in _UIK_RE.findall(link.text)}))
    if uik_matches and parent.oik_number is not None:
        return Scope(
            oik_number=parent.oik_number,
            region_name=parent.region_name,
            oik_name=parent.oik_name,
            tik_name=parent.tik_name,
            uik_numbers=uik_matches,
        )
    if link.hierarchy and parent.oik_number is None:
        return parent
    return None


def _party_list_pages(
    path: Path, plan: dict[str, Any], *, use_live_sources: bool = False
) -> list[PendingPage]:
    """Derive correctly scoped TIK result URLs from the verified 2021 UIK inventory."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
            if len(members) != 1:
                raise SourceError(f"{path} must contain exactly one CSV")
            with archive.open(members[0]) as source:
                rows = csv.DictReader(line.decode("utf-8") for line in source)
                grouped: dict[tuple[str, int, str, str], set[int]] = {}
                for row in rows:
                    match = _OIK_RE.search(row.get("oik", ""))
                    uik_match = _UIK_RE.search(row.get("uik", ""))
                    url = row.get("url", "").strip()
                    if not match or not uik_match or not url:
                        continue
                    key = (
                        row.get("region", "").strip(),
                        int(match.group(1)),
                        row.get("tik", "").strip(),
                        url,
                    )
                    grouped.setdefault(key, set()).add(int(uik_match.group(1)))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise SourceError(f"cannot read party-list UIK inventory {path}: {exc}") from exc
    pages = []
    for (region, oik, tik, url), uiks in sorted(grouped.items()):
        scope = Scope(oik, region, f"ОИК №{oik}", tik, tuple(sorted(uiks)))
        typed = _with_report_type(url, plan, scope)
        source_url = typed if use_live_sources else _rewrite_discovered(typed, plan)
        pages.append(PendingPage(source_url, scope, "tik"))
    if not pages:
        raise SourceError(f"party-list UIK inventory {path} yielded no crawl seeds")
    return pages


def _with_report_type(url: str, plan: dict[str, Any], scope: Scope) -> str:
    if scope.oik_number is None:
        return url
    report_type = plan.get("report_type")
    if report_type is None:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "type" for key, _ in query):
        query.append(("type", str(report_type)))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def _page_scope(scope: Scope, text: str) -> Scope:
    uiks = tuple(sorted(set(scope.uik_numbers) | {int(value) for value in _UIK_RE.findall(text)}))
    return Scope(
        oik_number=scope.oik_number,
        region_name=scope.region_name,
        oik_name=scope.oik_name,
        tik_name=scope.tik_name,
        uik_numbers=uiks,
    )


def _rewrite_discovered(url: str, plan: dict[str, Any]) -> str:
    rewrite = plan.get("rewrite_discovered")
    if not isinstance(rewrite, dict):
        return url
    suffix = str(rewrite.get("match_host_suffix", "")).casefold()
    prefix = rewrite.get("prefix")
    host = urllib.parse.urlsplit(url).hostname or ""
    if suffix and host.casefold().endswith(suffix) and isinstance(prefix, str):
        return f"{prefix}{url}"
    return url


def _is_crawlable(url: str, plan: dict[str, Any]) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file":
        return True
    hosts = {str(value).casefold() for value in plan.get("allowed_hosts", [])}
    return (
        parts.scheme in {"http", "https"}
        and parts.hostname is not None
        and (not hosts or parts.hostname.casefold() in hosts)
    )


def _target_path(raw_dir: Path, snapshot_key: str, page: PendingPage) -> Path:
    digest = hashlib.sha256(_canonical_url(page.url).encode()).hexdigest()[:20]
    if page.scope.oik_number is None:
        directory = "index"
    else:
        directory = f"oik-{page.scope.oik_number:03d}"
    return raw_dir / snapshot_key / directory / f"{digest}.html"


def _relative_path(path: Path, raw_dir: Path) -> str:
    try:
        return path.relative_to(raw_dir).as_posix()
    except ValueError as exc:
        raise SourceError(f"raw artifact path escapes {raw_dir}: {path}") from exc


def _existing_records(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.exists():
        return {}
    document = load_snapshot_manifest(manifest_path)
    return {_canonical_url(item["source_url"]): item for item in document["payloads"]}


def _verify_existing(record: dict[str, Any], raw_dir: Path) -> Path:
    relative = Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise SourceError(f"preserved payload path escapes raw directory: {relative}")
    path = raw_dir / relative
    if not path.is_file():
        raise SourceError(f"preserved payload is missing: {path}")
    if path.stat().st_size != record["size_bytes"]:
        raise SourceError(f"size mismatch for preserved payload: {path}")
    if sha256_file(path) != record["sha256"]:
        raise SourceError(f"checksum mismatch for preserved payload: {path}")
    return path


def _open_response(
    opener: Callable[..., BinaryIO], url: str, timeout: float
) -> tuple[BinaryIO, int | None, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "elections-workbench/0.1"})
    response = opener(request, timeout=timeout)
    status = getattr(response, "status", None)
    final_url = response.geturl()
    media_type = (
        response.headers.get_content_type() if response.headers else "application/octet-stream"
    )
    return response, status, final_url, media_type


def _record_for_payload(
    page: PendingPage,
    scope: Scope,
    target: Path,
    raw_dir: Path,
    *,
    source_url: str,
    final_url: str,
    status: str,
    media_type: str,
    http_status: int | None,
    retrieved_at: str,
) -> dict[str, Any]:
    return {
        "key": (
            f"{scope.oik_number or 'index'}-"
            f"{hashlib.sha256(_canonical_url(source_url).encode()).hexdigest()[:12]}"
        ),
        "oik_number": scope.oik_number,
        "region_name": scope.region_name,
        "oik_name": scope.oik_name,
        "tik_name": scope.tik_name,
        "uik_numbers": list(scope.uik_numbers),
        "relation": page.relation,
        "source_url": source_url,
        "final_url": final_url,
        "path": _relative_path(target, raw_dir),
        "retrieved_at": retrieved_at,
        "size_bytes": target.stat().st_size,
        "media_type": media_type,
        "sha256": sha256_file(target),
        "status": status,
        "http_status": http_status,
    }


def _font_source_url(page_url: str, final_url: str, relative_font: str) -> str:
    archive = re.match(r"(https?://web\.archive\.org/web/\d+(?:id_)?/)(https?://.*)", final_url)
    if archive:
        return archive.group(1) + urllib.parse.urljoin(archive.group(2), relative_font)
    return urllib.parse.urljoin(final_url or page_url, relative_font)


def _coverage(expected: list[dict[str, Any]], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    preserved = [item for item in payloads if item["status"] in PRESERVED_STATUSES]
    by_number: dict[int, dict[str, Any]] = {}
    for item in expected:
        number = item["number"]
        rows = [payload for payload in preserved if payload.get("oik_number") == number]
        tiks = sorted({row["tik_name"] for row in rows if row.get("tik_name")})
        uiks = sorted({uik for row in rows for uik in row.get("uik_numbers", [])})
        by_number[number] = {
            "oik_number": number,
            "region_name": next(
                (row["region_name"] for row in rows if row.get("region_name")),
                item.get("region_name"),
            ),
            "oik_name": next(
                (row["oik_name"] for row in rows if row.get("oik_name")), item.get("name")
            ),
            "status": "covered" if rows else "missing",
            "payloads": len(rows),
            "tiks": len(tiks),
            "uiks": len(uiks),
        }
    covered = sum(item["status"] == "covered" for item in by_number.values())
    return {
        "expected_oiks": len(expected),
        "covered_oiks": covered,
        "missing_oiks": len(expected) - covered,
        "tiks": len(
            {
                (row.get("region_name"), row.get("oik_number"), row["tik_name"])
                for row in preserved
                if row.get("tik_name")
            }
        ),
        "uiks": len(
            {
                (row.get("region_name"), uik)
                for row in preserved
                for uik in row.get("uik_numbers", [])
            }
        ),
        "by_oik": list(by_number.values()),
    }


def _missing_oik_gaps(
    expected: list[dict[str, Any]], coverage: dict[str, Any], existing: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    gaps = list(existing)
    already_missing = {
        item.get("oik_number")
        for item in gaps
        if item.get("status") == "unavailable" and item.get("detail") == "no preserved payload"
    }
    for item in coverage["by_oik"]:
        if item["status"] == "missing" and item["oik_number"] not in already_missing:
            expected_row = next(row for row in expected if row["number"] == item["oik_number"])
            gaps.append(
                {
                    "oik_number": item["oik_number"],
                    "region_name": expected_row.get("region_name"),
                    "oik_name": expected_row.get("name"),
                    "status": "unavailable",
                    "detail": "no preserved payload",
                }
            )
    return gaps


def acquire_snapshot(
    plan_path: Path,
    raw_dir: Path,
    manifest_path: Path,
    *,
    force: bool = False,
    rate_limit_seconds: float | None = None,
    timeout: float = 120,
    max_pages: int | None = None,
    party_list_archive: Path | None = None,
    use_live_sources: bool = False,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    plan = load_acquisition_plan(plan_path)
    existing = {} if force else _existing_records(manifest_path)
    delay = (
        float(rate_limit_seconds)
        if rate_limit_seconds is not None
        else float(plan.get("rate_limit_seconds", 1.0))
    )
    if delay < 0:
        raise SourceError("rate_limit_seconds cannot be negative")

    queue: deque[PendingPage] = deque()
    expected_by_number = {item["number"]: item for item in plan["expected_oiks"]}
    if party_list_archive is not None:
        queue.extend(_party_list_pages(party_list_archive, plan, use_live_sources=use_live_sources))
    for seed in [] if party_list_archive is not None else plan["seeds"]:
        scope = Scope(
            oik_number=seed.get("oik_number"),
            region_name=seed.get("region_name"),
            oik_name=seed.get("oik_name"),
            tik_name=seed.get("tik_name"),
        )
        queue.append(PendingPage(seed["url"], scope, str(seed.get("relation", "index"))))

    seen: set[str] = set()
    payloads: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    network_requests = 0
    reused_payloads = 0
    page_limit = int(max_pages) if max_pages is not None else int(plan.get("max_pages", 20_000))
    if page_limit < 1:
        raise SourceError("max_pages must be positive")
    while queue:
        page = queue.popleft()
        canonical = _canonical_url(page.url)
        if canonical in seen:
            continue
        seen.add(canonical)
        if len(seen) > page_limit:
            gaps.append(
                {
                    "status": "inconsistent",
                    "source_url": page.url,
                    "detail": f"discovery exceeded max_pages={page_limit}",
                }
            )
            break

        old = existing.get(canonical)
        if old is not None:
            try:
                target = _verify_existing(old, raw_dir)
            except SourceError:
                raise SourceError(
                    "a preserved payload changed; rerun with --force to replace the snapshot"
                ) from None
            record = dict(old)
            payload = target.read_bytes()
            media_type = record["media_type"]
            reused_payloads += 1
        else:
            if network_requests and delay:
                sleeper(delay)
            network_requests += 1
            target = _target_path(raw_dir, plan["snapshot_key"], page)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f".{target.name}.part")
            try:
                response, http_status, final_url, media_type = _open_response(
                    opener, page.url, timeout
                )
                with response, partial.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                if partial.stat().st_size == 0:
                    raise SourceError("empty response")
                os.replace(partial, target)
                status = (
                    "redirected"
                    if _canonical_url(final_url) != _canonical_url(page.url)
                    else "preserved"
                )
                record = _record_for_payload(
                    page,
                    page.scope,
                    target,
                    raw_dir,
                    source_url=page.url,
                    final_url=final_url,
                    status=status,
                    media_type=media_type,
                    http_status=http_status,
                    retrieved_at=_utc_now(),
                )
            except (OSError, SourceError, urllib.error.URLError) as exc:
                partial.unlink(missing_ok=True)
                gaps.append(
                    {
                        "oik_number": page.scope.oik_number,
                        "region_name": page.scope.region_name,
                        "oik_name": page.scope.oik_name,
                        "tik_name": page.scope.tik_name,
                        "source_url": page.url,
                        "status": "unavailable",
                        "detail": str(exc),
                    }
                )
                continue
            payload = target.read_bytes()

        html = _decoded_html(payload, media_type)
        font_match = _FONT_RE.search(html or "")
        if font_match:
            font_target = target.with_suffix(".ttf")
            font_source = _font_source_url(page.url, record["final_url"], font_match.group(1))
            font_valid = (
                font_target.is_file()
                and record.get("font_sha256") == sha256_file(font_target)
                and record.get("font_size_bytes") == font_target.stat().st_size
            )
            if not font_valid:
                if network_requests and delay:
                    sleeper(delay)
                network_requests += 1
                partial_font = font_target.with_name(f".{font_target.name}.part")
                try:
                    font_response, _, _, _ = _open_response(opener, font_source, timeout)
                    with font_response, partial_font.open("wb") as output:
                        while chunk := font_response.read(1024 * 1024):
                            output.write(chunk)
                    if partial_font.stat().st_size == 0:
                        raise SourceError("empty CEC font response")
                    os.replace(partial_font, font_target)
                except (OSError, SourceError, urllib.error.URLError) as exc:
                    partial_font.unlink(missing_ok=True)
                    record["status"] = "malformed"
                    gaps.append(
                        {
                            "oik_number": page.scope.oik_number,
                            "source_url": page.url,
                            "status": "malformed",
                            "detail": f"cannot preserve CEC deobfuscation font: {exc}",
                        }
                    )
            if font_target.is_file():
                record.update(
                    {
                        "font_source_url": font_source,
                        "font_path": _relative_path(font_target, raw_dir),
                        "font_size_bytes": font_target.stat().st_size,
                        "font_sha256": sha256_file(font_target),
                    }
                )

        try:
            links, text = _parse_links(payload, media_type, record["final_url"])
        except SourceError as exc:
            record["status"] = "malformed"
            gaps.append(
                {
                    "oik_number": page.scope.oik_number,
                    "region_name": page.scope.region_name,
                    "source_url": page.url,
                    "status": "malformed",
                    "detail": str(exc),
                }
            )
            payloads.append(record)
            continue

        scope = _page_scope(page.scope, text)
        if scope.oik_number is not None and not 1 <= scope.oik_number <= 225:
            record["status"] = "inconsistent"
            gaps.append(
                {
                    "oik_number": scope.oik_number,
                    "source_url": page.url,
                    "status": "inconsistent",
                    "detail": "OIK number is outside 1..225",
                }
            )
        record.update(
            {
                "oik_number": scope.oik_number,
                "region_name": scope.region_name,
                "oik_name": scope.oik_name,
                "tik_name": scope.tik_name,
                "uik_numbers": list(scope.uik_numbers),
            }
        )
        payloads.append(record)
        if record["status"] == "redirected":
            gaps.append(
                {
                    "oik_number": scope.oik_number,
                    "region_name": scope.region_name,
                    "source_url": page.url,
                    "final_url": record["final_url"],
                    "status": "redirected",
                    "detail": "source redirected; response was preserved",
                }
            )

        for link in links:
            child_scope = _scope_from_link(scope, link, expected_by_number)
            if child_scope is None:
                continue
            child_url = _with_report_type(link.url, plan, child_scope)
            child_url = _rewrite_discovered(child_url, plan)
            if not _is_crawlable(child_url, plan):
                gaps.append(
                    {
                        "oik_number": child_scope.oik_number,
                        "region_name": child_scope.region_name,
                        "source_url": child_url,
                        "status": "inconsistent",
                        "detail": "discovered URL is outside allowed hosts",
                    }
                )
                continue
            relation = (
                "uik"
                if child_scope.uik_numbers
                else "tik"
                if child_scope.tik_name
                else "oik"
                if child_scope.oik_number is not None
                else "index"
            )
            pending = PendingPage(child_url, child_scope, relation)
            if child_scope.oik_number is not None and scope.oik_number is None:
                queue.appendleft(pending)
            else:
                queue.append(pending)

    expected = plan["expected_oiks"]
    coverage = _coverage(expected, payloads)
    gaps = _missing_oik_gaps(expected, coverage, gaps)
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_key": plan["snapshot_key"],
        "source": plan.get("source", {}),
        "generated_at": _utc_now(),
        "raw_root": str(raw_dir),
        "expected_oiks": expected,
        "payloads": sorted(
            payloads,
            key=lambda item: (
                item.get("oik_number") or 0,
                item.get("tik_name") or "",
                item["source_url"],
            ),
        ),
        "coverage": coverage,
        "gaps": gaps,
        "run": {"network_requests": network_requests, "reused_payloads": reused_payloads},
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    partial_manifest = manifest_path.with_name(f".{manifest_path.name}.part")
    partial_manifest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(partial_manifest, manifest_path)
    return document


def load_snapshot_manifest(path: Path) -> dict[str, Any]:
    document = _read_json(path, "snapshot manifest")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SourceError(f"unsupported snapshot manifest version in {path}")
    if not isinstance(document.get("expected_oiks"), list):
        raise SourceError(f"snapshot manifest {path} does not enumerate expected_oiks")
    if not isinstance(document.get("payloads"), list):
        raise SourceError(f"snapshot manifest {path} does not contain payloads")
    required = {
        "key",
        "oik_number",
        "region_name",
        "source_url",
        "final_url",
        "path",
        "retrieved_at",
        "size_bytes",
        "media_type",
        "sha256",
        "status",
    }
    keys: set[str] = set()
    urls: set[str] = set()
    for record in document["payloads"]:
        if not isinstance(record, dict):
            raise SourceError(f"snapshot manifest {path} contains a non-object payload")
        missing = sorted(required - record.keys())
        if missing:
            raise SourceError(f"snapshot payload is missing {', '.join(missing)}")
        if record["key"] in keys:
            raise SourceError(f"duplicate snapshot payload key: {record['key']}")
        keys.add(record["key"])
        canonical = _canonical_url(record["source_url"])
        if canonical in urls:
            raise SourceError(f"duplicate snapshot payload URL: {record['source_url']}")
        urls.add(canonical)
        if record["status"] not in PRESERVED_STATUSES | GAP_STATUSES:
            raise SourceError(f"unsupported snapshot payload status: {record['status']}")
        if record["oik_number"] is not None and not record["region_name"]:
            raise SourceError(f"OIK payload {record['key']} has no region_name")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"])):
            raise SourceError(f"snapshot payload {record['key']} has an invalid SHA-256")
    gaps = document.get("gaps")
    if not isinstance(gaps, list):
        raise SourceError(f"snapshot manifest {path} does not contain a gap report")
    for gap in gaps:
        if not isinstance(gap, dict) or gap.get("status") not in GAP_STATUSES:
            raise SourceError(f"snapshot manifest {path} contains an invalid gap record")
    return document


def verify_snapshot(
    manifest_path: Path, raw_dir: Path, *, require_complete: bool = True
) -> dict[str, Any]:
    document = load_snapshot_manifest(manifest_path)
    failures: list[str] = []
    for record in document["payloads"]:
        try:
            _verify_existing(record, raw_dir)
        except (KeyError, SourceError) as exc:
            failures.append(str(exc))
    coverage = _coverage(document["expected_oiks"], document["payloads"])
    if document.get("coverage") != coverage:
        failures.append("stored coverage does not match payload provenance")
    if require_complete and coverage["missing_oiks"]:
        failures.append(f"{coverage['missing_oiks']} expected OIKs have no preserved payload")
    if require_complete:
        blockers = [gap for gap in document["gaps"] if gap["status"] in BLOCKING_GAP_STATUSES]
        if blockers:
            failures.append(f"gap report contains {len(blockers)} blocking entries")
    if failures:
        raise SourceError("snapshot verification failed: " + "; ".join(failures))
    return {
        "snapshot_key": document.get("snapshot_key"),
        "verified_payloads": len(document["payloads"]),
        "coverage": coverage,
    }
