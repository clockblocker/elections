"""Small interface over the CEC lookup's unstable HTTP implementation.

The public lookup site has changed paths and response wrappers over time.  This
module deliberately makes the request shape data, while keeping response
interpretation conservative: a suggestion is resolvable only when the response
contains both an address label and a commission identifier.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


class CecError(RuntimeError):
    """Base class for an expected CEC lookup failure."""


class CecTransportError(CecError):
    """The remote endpoint could not be reached or returned an HTTP error."""


class CecResponseError(CecError):
    """The endpoint response did not contain the required evidence."""


@dataclass(frozen=True, slots=True)
class CecProtocol:
    """Captured request recipe for one version of the public lookup."""

    suggest_url: str
    query_parameter: str = "query"
    method: str = "GET"
    static_parameters: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = (("Accept", "application/json"),)
    committee_url: str | None = None
    resolve_url: str | None = None

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> CecProtocol:
        def pairs(name: str) -> tuple[tuple[str, str], ...]:
            value = payload.get(name, {})
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be an object")
            return tuple(sorted((str(key), str(item)) for key, item in value.items()))

        suggest_url = payload.get("suggest_url")
        if not isinstance(suggest_url, str) or not suggest_url.startswith(("http://", "https://")):
            raise ValueError("suggest_url must be an absolute HTTP(S) URL")
        method = str(payload.get("method", "GET")).upper()
        if method not in {"GET", "POST"}:
            raise ValueError("method must be GET or POST")
        return cls(
            suggest_url=suggest_url,
            query_parameter=str(payload.get("query_parameter", "query")),
            method=method,
            static_parameters=pairs("static_parameters"),
            headers=pairs("headers") or (("Accept", "application/json"),),
            committee_url=(str(payload["committee_url"]) if payload.get("committee_url") else None),
            resolve_url=(str(payload["resolve_url"]) if payload.get("resolve_url") else None),
        )


@dataclass(frozen=True, slots=True)
class CecSuggestion:
    label: str
    commission_id: str | None
    address_id: str | None
    region_code: str | None
    leaf: bool | None
    raw: Mapping[str, Any]

    @property
    def is_resolved(self) -> bool:
        return self.commission_id is not None


@dataclass(frozen=True, slots=True)
class CecResponse:
    url: str
    status: int
    body: bytes
    suggestions: tuple[CecSuggestion, ...]


@dataclass(frozen=True, slots=True)
class CecCommittee:
    """Fields required for a house→polling-place assignment."""

    region_code: str
    uik_number: int
    vrn: str | None
    name: str
    commission_address: str | None
    polling_place_address: str
    telephone: str | None


@dataclass(frozen=True, slots=True)
class CecTreeNode:
    """One address node returned by the legacy CEC jsTree endpoint."""

    node_id: str
    text: str
    address_id: str | None
    level_id: str | None
    result_token: str | None
    has_children: bool
    raw: Mapping[str, Any]


Transport = Callable[[Request, float], tuple[int, bytes]]


class _UrlopenTransport:
    """HTTP transport retaining cookies for one lookup/crawl session."""

    def __init__(self) -> None:
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def __call__(self, request: Request, timeout: float) -> tuple[int, bytes]:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return int(response.status), response.read()
        except HTTPError as error:
            body = error.read()
            raise CecTransportError(f"CEC returned HTTP {error.code}: {body[:200]!r}") from error
        except (URLError, TimeoutError) as error:
            raise CecTransportError(f"CEC request failed: {error}") from error


class CecLookup:
    """Resolve a canonical address query using a captured CEC protocol recipe."""

    def __init__(
        self,
        protocol: CecProtocol,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        attempts: int = 3,
    ) -> None:
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        self._protocol = protocol
        self._timeout = timeout
        self._transport = transport or _UrlopenTransport()
        self._attempts = attempts

    @property
    def source_url(self) -> str:
        """Captured lookup URL used as provenance when transport fails."""

        return self._protocol.suggest_url

    @classmethod
    def from_protocol_file(cls, path: str, *, timeout: float = 30.0) -> CecLookup:
        with open(path, encoding="utf-8") as source:
            payload = json.load(source)
        if not isinstance(payload, Mapping):
            raise TypeError("protocol file must contain a JSON object")
        return cls(CecProtocol.from_json(payload), timeout=timeout)

    def suggest(self, address: str) -> CecResponse:
        query = address.strip()
        if not query:
            raise ValueError("address must not be empty")

        parameters = dict(self._protocol.static_parameters)
        parameters[self._protocol.query_parameter] = query
        encoded = urlencode(parameters).encode("utf-8")
        if self._protocol.method == "GET":
            if "{query}" in self._protocol.suggest_url:
                url = self._protocol.suggest_url.format(query=quote(query, safe=""))
                if self._protocol.static_parameters:
                    separator = "&" if "?" in url else "?"
                    url = f"{url}{separator}{urlencode(dict(self._protocol.static_parameters))}"
            else:
                separator = "&" if "?" in self._protocol.suggest_url else "?"
                url = f"{self._protocol.suggest_url}{separator}{encoded.decode('ascii')}"
            body = None
        else:
            url = self._protocol.suggest_url
            body = encoded

        headers = dict(self._protocol.headers)
        if body is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        request = Request(url, data=body, headers=headers, method=self._protocol.method)
        status, response_body = self._request(request)
        try:
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CecResponseError("CEC response was not JSON") from error
        suggestions = tuple(_extract_suggestions(payload))
        return CecResponse(url=url, status=status, body=response_body, suggestions=suggestions)

    def resolve(self, address_id: str) -> tuple[str, int, bytes, CecCommittee]:
        """Resolve a terminal address identifier to a UIK and polling room."""

        template = self._protocol.resolve_url
        if not template:
            raise CecError("resolve_url is not configured in the captured protocol")
        if not address_id.strip():
            raise ValueError("address_id must not be empty")
        url = template.format(address_id=quote(address_id.strip(), safe=""))
        request = Request(url, headers=dict(self._protocol.headers), method="GET")
        status, body = self._request(request)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CecResponseError("CEC committee response was not JSON") from error
        if not isinstance(payload, Mapping):
            raise CecResponseError("CEC committee response was not an object")
        return url, status, body, _parse_committee(payload)

    def committee(self, region_code: str, committee_number: str) -> tuple[str, int, bytes]:
        template = self._protocol.committee_url
        if not template:
            raise CecError("committee_url is not configured in the captured protocol")
        url = template.format(
            region_code=quote(region_code.strip(), safe=""),
            committee_number=quote(committee_number.strip(), safe=""),
        )
        request = Request(url, headers=dict(self._protocol.headers), method="GET")
        status, body = self._request(request)
        return url, status, body

    def _request(self, request: Request) -> tuple[int, bytes]:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._transport(request, self._timeout)
            except CecTransportError:
                if attempt == self._attempts:
                    raise
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable retry loop")


class CecLegacyTree:
    """Walk the 2016-2018 CEC hierarchical address classifier.

    Node IDs are deliberately treated as opaque and snapshot-local.  Callers
    should persist the raw body for every request and use ``address_id`` as
    evidence, never attempt to manufacture descendant IDs.
    """

    def __init__(
        self,
        base_url: str = "http://www.cikrf.ru",
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        attempts: int = 3,
    ) -> None:
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or _UrlopenTransport()
        self._attempts = attempts

    def roots(self) -> tuple[str, int, bytes, tuple[CecTreeNode, ...]]:
        return self._nodes(f"{self._base_url}/services/lk_tree/?first=1")

    def children(
        self, node_id: str, *, result_token: str | None = None
    ) -> tuple[str, int, bytes, tuple[CecTreeNode, ...]]:
        if not node_id.strip():
            raise ValueError("node_id must not be empty")
        parameters = {"id": node_id}
        if result_token is not None:
            parameters["ret"] = result_token
        url = f"{self._base_url}/services/lk_tree/?{urlencode(parameters)}"
        return self._nodes(url)

    def result(self, address_id: str) -> tuple[str, int, bytes]:
        if not address_id.strip():
            raise ValueError("address_id must not be empty")
        url = f"{self._base_url}/services/lk_address/{quote(address_id.strip(), safe='')}?do=result"
        request = Request(url, headers={"Accept": "text/html"}, method="GET")
        status, body = self._request(request)
        return url, status, body

    def _nodes(self, url: str) -> tuple[str, int, bytes, tuple[CecTreeNode, ...]]:
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        status, body = self._request(request)
        payload: Any = None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "windows-1251"):
            try:
                payload = json.loads(body.decode(encoding))
                break
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                last_error = error
        if not isinstance(payload, list):
            raise CecResponseError("legacy tree response was not a JSON array") from last_error
        nodes = self._parse_nodes(payload)
        # The initial legacy response commonly wrapped all subjects in a
        # synthetic ``Россия`` node whose ``children`` value was the actual
        # array.  jsTree rendered those children without another HTTP request.
        # Expose them as roots to callers, rather than treating the wrapper as
        # an ordinary lazily expandable node.
        if len(payload) == 1 and isinstance(payload[0], Mapping):
            embedded = payload[0].get("children")
            if isinstance(embedded, list):
                nodes = self._parse_nodes(embedded)
        return url, status, body, tuple(nodes)

    @staticmethod
    def _parse_nodes(payload: list[Any]) -> list[CecTreeNode]:
        nodes: list[CecTreeNode] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            node_id = _first_scalar(item, "id")
            text = _first_text(item, "text")
            attributes = item.get("a_attr")
            if not node_id or not text or not isinstance(attributes, Mapping):
                continue
            nodes.append(
                CecTreeNode(
                    node_id=node_id,
                    text=text,
                    address_id=_first_scalar(attributes, "intid"),
                    level_id=_first_scalar(attributes, "levelid"),
                    result_token=_first_scalar(attributes, "ret"),
                    has_children=bool(item.get("children")),
                    raw=item,
                )
            )
        return nodes

    def _request(self, request: Request) -> tuple[int, bytes]:
        for attempt in range(1, self._attempts + 1):
            try:
                return self._transport(request, self._timeout)
            except CecTransportError:
                if attempt == self._attempts:
                    raise
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable retry loop")


def _extract_suggestions(payload: Any) -> Iterable[CecSuggestion]:
    """Yield only records that carry an address-like label.

    Known CEC-era clients used several wrappers.  Walking JSON objects makes the
    adapter tolerate those wrappers without pretending that arbitrary fields are
    equivalent: identifiers are taken only from a short, explicit key set.
    """

    seen: set[tuple[str, str | None, str | None]] = set()
    for item in _objects(payload):
        nested = item.get("data") if isinstance(item.get("data"), Mapping) else {}
        label = _first_text(item, "value", "label", "name", "fullname", "fullName", "address")
        if label is None:
            label = _first_text(nested, "fullname", "fullName", "address", "value")
        if label is None:
            continue
        commission = _first_scalar(
            item,
            "commission_id",
            "commissionId",
            "committee_id",
            "committeeId",
            "uik_id",
            "uikId",
        ) or _first_scalar(
            nested,
            "commission_id",
            "commissionId",
            "committee_id",
            "committeeId",
            "uik_id",
            "uikId",
        )
        address_id = _first_scalar(item, "intid", "address_id", "addressId", "id") or _first_scalar(
            nested, "intid", "address_id", "addressId", "id"
        )
        region = _first_scalar(item, "region_id", "regionId", "subjCode") or _first_scalar(
            nested, "region_id", "regionId", "subjCode"
        )
        key = (label, commission, address_id)
        if key in seen:
            continue
        seen.add(key)
        leaf_value = item.get("leaf", nested.get("leaf"))
        leaf = leaf_value if isinstance(leaf_value, bool) else None
        yield CecSuggestion(label, commission, address_id, region, leaf, item)


def _objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _first_text(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _first_scalar(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, (str, int)) and str(item).strip():
            return str(item).strip()
    return None


_UIK_NUMBER_RE = re.compile(r"№\s*([0-9]+)")


def _parse_committee(payload: Mapping[str, Any]) -> CecCommittee:
    name = _first_text(payload, "name")
    region = _first_scalar(payload, "subjCode", "regionCode", "region_id")
    if not name or not region:
        raise CecResponseError("CEC committee response lacks name or region code")
    number_value = _first_scalar(payload, "number", "uikNumber", "committeeNumber")
    match = _UIK_NUMBER_RE.search(name)
    try:
        number = (
            int(number_value) if number_value is not None else int(match.group(1)) if match else 0
        )
    except ValueError as error:
        raise CecResponseError("CEC committee number is not numeric") from error
    if number <= 0:
        raise CecResponseError("CEC committee response lacks a positive UIK number")
    commission = payload.get("address") if isinstance(payload.get("address"), Mapping) else {}
    voting = (
        payload.get("votingAddress") if isinstance(payload.get("votingAddress"), Mapping) else {}
    )
    polling_address = _first_text(voting, "address")
    if polling_address is None:
        raise CecResponseError("CEC committee response lacks a voting-place address")
    return CecCommittee(
        region_code=region,
        uik_number=number,
        vrn=_first_scalar(payload, "vrn"),
        name=name,
        commission_address=_first_text(commission, "address"),
        polling_place_address=polling_address,
        telephone=_first_text(voting, "phone") or _first_text(commission, "phone"),
    )
