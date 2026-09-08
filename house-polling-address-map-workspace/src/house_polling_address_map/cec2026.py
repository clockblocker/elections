"""Live 2026 CEC gateway probe.

The 2026 public page uses an unauthenticated GraphQL address index followed by
the authenticated public ``ik-inp-service-pbcopy`` gateway.  Authentication is
an arithmetic browser challenge.  Challenge responses are intentionally never
stored because the solve response contains an ephemeral API key.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .store import PollingMapStore

_CHALLENGE_RE = re.compile(r"^\s*return\s+(-?\d+)\s*\+\s*(-?\d+)\s*;?\s*$")
_SEARCH_QUERY = """query ($query: String!) {
  ADDRESSSearchFulltext(
    query: $query
    limit: 20
    incomplete: true
    unchecked: false
    locations: ["1"]
  ) {
    id
    fullName
    isRetro
  }
}"""
_PARENTS_QUERY = """query ($query: ID!) {
  nodeADDRESSById(id: $query) {
    id
    parents {
      id
      VCADDRESS_NAME
    }
  }
}"""


class Cec2026Error(RuntimeError):
    """A safe-to-display live-gateway failure."""


@dataclass(frozen=True, slots=True)
class Cec2026Protocol:
    address_graphql_url: str
    api_url: str
    election_id: int
    election_external_id: str | None = None
    voting_date: str | None = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Cec2026Protocol:
        address_url = str(value.get("address_graphql_url") or "")
        api_url = str(value.get("api_url") or "")
        if not address_url.startswith(("http://", "https://")):
            raise ValueError("address_graphql_url must be an absolute HTTP(S) URL")
        if not api_url.startswith(("http://", "https://")):
            raise ValueError("api_url must be an absolute HTTP(S) URL")
        try:
            election_id = int(value["election_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("election_id must be a positive integer") from error
        if election_id <= 0:
            raise ValueError("election_id must be a positive integer")
        return cls(
            address_graphql_url=address_url,
            api_url=api_url.rstrip("/"),
            election_id=election_id,
            election_external_id=(
                str(value["election_external_id"])
                if value.get("election_external_id")
                else None
            ),
            voting_date=str(value["voting_date"]) if value.get("voting_date") else None,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> Cec2026Protocol:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError("2026 protocol file must contain a JSON object")
        return cls.from_json(value)


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    step: str
    url: str
    status: int
    sha256: str
    body: bytes


@dataclass(frozen=True, slots=True)
class AddressCandidate:
    address_id: str
    full_name: str
    is_retro: bool


@dataclass(frozen=True, slots=True)
class Cec2026ProbeResult:
    query: str
    candidates: tuple[AddressCandidate, ...]
    selected_address_id: str | None
    address_ids: tuple[str, ...]
    election: Mapping[str, Any] | None
    uiks: tuple[Mapping[str, Any], ...]
    commission_orgs: tuple[Mapping[str, Any], ...]
    captures: tuple[CapturedResponse, ...]


class Cec2026Probe:
    """Reproduce the public 2026 browser sequence and preserve public bodies."""

    def __init__(
        self,
        protocol: Cec2026Protocol,
        store: PollingMapStore,
        *,
        proxy_url: str | None = None,
        timeout: float = 30.0,
        session: Any | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.protocol = protocol
        self.store = store
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
        self._api_key = ""

    @property
    def proxies(self) -> Mapping[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def probe(self, query: str, *, address_id: str | None = None) -> Cec2026ProbeResult:
        candidates, search_capture = self._search(query)
        selected = address_id.strip() if address_id else None
        if not selected:
            current = tuple(candidate for candidate in candidates if not candidate.is_retro)
            if len(current) == 1:
                selected = current[0].address_id
        if not selected:
            return Cec2026ProbeResult(
                query,
                candidates,
                None,
                (),
                None,
                (),
                (),
                (search_capture,),
            )

        address_ids, parent_capture = self._parents(selected)
        elections, elections_capture = self._api_json(
            "addresses/elections",
            {"page": 1, "perPage": 100, "addressIds": ",".join(address_ids)},
            step="elections",
        )
        content = elections.get("content", []) if isinstance(elections, Mapping) else []
        election = next(
            (
                item
                for item in content
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == str(self.protocol.election_id)
            ),
            None,
        )
        self._validate_election(election)
        classifiers, classifier_capture = self._api_json(
            "addresses/commissionClassifiers",
            {"addressIds": ",".join(address_ids), "electionsId": self.protocol.election_id},
            step="commission-classifiers",
        )
        uiks_value = classifiers.get("uik", []) if isinstance(classifiers, Mapping) else []
        uiks = tuple(item for item in uiks_value if isinstance(item, Mapping))
        organizations, organizations_capture = self._api_json(
            "addresses/commissionOrgs",
            {"page": 1, "perPage": 100, "addressIds": ",".join(address_ids)},
            step="commission-organizations",
        )
        organizations_value = (
            organizations.get("content", []) if isinstance(organizations, Mapping) else []
        )
        commission_orgs = tuple(
            item for item in organizations_value if isinstance(item, Mapping)
        )
        return Cec2026ProbeResult(
            query,
            candidates,
            selected,
            address_ids,
            election,
            uiks,
            commission_orgs,
            (
                search_capture,
                parent_capture,
                elections_capture,
                classifier_capture,
                organizations_capture,
            ),
        )

    def _validate_election(self, election: Mapping[str, Any] | None) -> None:
        if election is None:
            raise Cec2026Error(
                f"configured election {self.protocol.election_id} was not returned for the address"
            )
        expected = {
            "externalId": self.protocol.election_external_id,
            "votingDate": self.protocol.voting_date,
        }
        for field, expected_value in expected.items():
            if expected_value is not None and str(election.get(field) or "") != expected_value:
                raise Cec2026Error(
                    f"configured election {self.protocol.election_id} has unexpected {field}"
                )

    def _search(self, query: str) -> tuple[tuple[AddressCandidate, ...], CapturedResponse]:
        if not query.strip():
            raise ValueError("address query must not be empty")
        value, capture = self._graphql(
            _SEARCH_QUERY, {"query": query.strip()}, step="address-search"
        )
        rows = value.get("ADDRESSSearchFulltext", []) if isinstance(value, Mapping) else []
        candidates = tuple(
            AddressCandidate(str(row["id"]), str(row["fullName"]), bool(row.get("isRetro")))
            for row in rows
            if isinstance(row, Mapping) and row.get("id") and row.get("fullName")
        )
        return candidates, capture

    def _parents(self, address_id: str) -> tuple[tuple[str, ...], CapturedResponse]:
        value, capture = self._graphql(
            _PARENTS_QUERY, {"query": address_id}, step="address-parents"
        )
        node = value.get("nodeADDRESSById") if isinstance(value, Mapping) else None
        if not isinstance(node, Mapping):
            raise Cec2026Error("2026 address service returned no selected node")
        parent_rows = node.get("parents", [])
        parents = tuple(
            str(row["id"])
            for row in parent_rows
            if isinstance(row, Mapping) and row.get("id") is not None
        )
        return (address_id, *parents), capture

    def _graphql(
        self, document: str, variables: Mapping[str, Any], *, step: str
    ) -> tuple[Mapping[str, Any], CapturedResponse]:
        payload = {"query": document, "variables": dict(variables)}
        response = self._send(
            "POST",
            self.protocol.address_graphql_url,
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        capture = self._capture(step, response)
        value = self._json_object(response, step)
        errors = value.get("errors")
        if errors:
            raise Cec2026Error(f"{step} returned GraphQL errors")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise Cec2026Error(f"{step} returned no GraphQL data")
        return data, capture

    def _api_json(
        self, path: str, parameters: Mapping[str, Any], *, step: str
    ) -> tuple[Mapping[str, Any], CapturedResponse]:
        query = urllib.parse.urlencode(
            sorted((str(key), str(value)) for key, value in parameters.items())
        )
        url = f"{self.protocol.api_url}/{path.lstrip('/')}?{query}"
        response = self._send(
            "GET",
            url,
            headers={
                "X-Api-Key": self._authenticate(),
                "X-Client-Fingerprint": self.user_agent,
            },
        )
        capture = self._capture(step, response)
        return self._json_object(response, step), capture

    def _authenticate(self) -> str:
        if self._api_key:
            return self._api_key
        challenge = self._send("GET", f"{self.protocol.api_url}/challenge/get")
        value = self._json_object(challenge, "challenge/get")
        task = str(value.get("jsTask") or "")
        match = _CHALLENGE_RE.fullmatch(task)
        public_token = str(value.get("pubToken") or "")
        if not match or not public_token:
            raise Cec2026Error("unsupported 2026 CEC arithmetic challenge")
        answer = str(int(match.group(1)) + int(match.group(2)))
        solved = self._send(
            "POST",
            f"{self.protocol.api_url}/challenge/solve",
            headers={"Content-Type": "application/json"},
            json={
                "pubToken": public_token,
                "answer": answer,
                "fingerprint": self.user_agent,
            },
        )
        result = self._json_object(solved, "challenge/solve")
        self._api_key = str(result.get("apiKey") or "")
        if not self._api_key:
            raise Cec2026Error("2026 CEC challenge returned no API key")
        return self._api_key

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "http://apps.cikrf.ru",
            "Referer": "http://apps.cikrf.ru/service/gateway/?origin=http://www.cikrf.ru",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        try:
            response = self.session.request(
                method,
                url,
                headers=request_headers,
                json=json,
                proxies=self.proxies,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as error:
            raise Cec2026Error(self._safe_error(error)) from error
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise Cec2026Error(f"2026 CEC {urllib.parse.urlsplit(url).path} returned HTTP {status}")
        return response

    def _capture(self, step: str, response: Any) -> CapturedResponse:
        body = bytes(getattr(response, "content", b""))
        content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        digest = self.store.put_raw_response(body, media_type=content_type or None)
        return CapturedResponse(
            step=step,
            url=str(getattr(response, "url", "")),
            status=int(getattr(response, "status_code", 0) or 0),
            sha256=digest,
            body=body,
        )

    @staticmethod
    def _json_object(response: Any, step: str) -> Mapping[str, Any]:
        try:
            value = json.loads(bytes(getattr(response, "content", b"")))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Cec2026Error(f"{step} returned non-JSON") from error
        if not isinstance(value, Mapping):
            raise Cec2026Error(f"{step} returned a non-object JSON value")
        return value

    def _safe_error(self, error: BaseException) -> str:
        message = f"{type(error).__name__}: {error}"
        if self._api_key:
            message = message.replace(self._api_key, "<ephemeral API key>")
        if not self.proxy_url:
            return message
        message = message.replace(self.proxy_url, "<configured proxy>")
        parsed = urllib.parse.urlsplit(self.proxy_url)
        for secret in (parsed.username, parsed.password):
            if secret:
                message = message.replace(secret, "***")
                message = message.replace(urllib.parse.quote(secret, safe=""), "***")
        return message


def probe_result_json(result: Cec2026ProbeResult) -> dict[str, Any]:
    """Convert a probe result to safe JSON without embedding raw bodies."""

    return {
        "query": result.query,
        "candidates": [
            {
                "address_id": item.address_id,
                "full_name": item.full_name,
                "is_retro": item.is_retro,
            }
            for item in result.candidates
        ],
        "selected_address_id": result.selected_address_id,
        "address_ids": list(result.address_ids),
        "election": dict(result.election) if result.election else None,
        "uiks": [dict(item) for item in result.uiks],
        "commission_orgs": [dict(item) for item in result.commission_orgs],
        "captures": [
            {
                "step": item.step,
                "url": item.url,
                "status": item.status,
                "sha256": item.sha256,
            }
            for item in result.captures
        ],
        "probed_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "AddressCandidate",
    "CapturedResponse",
    "Cec2026Error",
    "Cec2026Probe",
    "Cec2026ProbeResult",
    "Cec2026Protocol",
    "probe_result_json",
]
