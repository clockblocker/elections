from __future__ import annotations

import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from typing import Any

try:
    from .common import decode_text
except ImportError:
    from common import decode_text


SPACE_RE = re.compile(r"\s+")
BALLOT_POSITION_RE = re.compile(r"^\s*\d+\s*\.\s*")
DISTRICT_NUMBER_RE = re.compile(r"/(\d+)\s*$")


def normalized_text(value: str) -> str:
    """Normalize presentation-only GAS differences without correcting source text."""
    value = unicodedata.normalize("NFKC", value).replace('\\"', '"')
    return SPACE_RE.sub(" ", value.replace("\u00a0", " ")).strip()


def normalized_full_name(value: str) -> str:
    """Return the strict full-name join key used within one official contest."""
    return normalized_text(value).replace("Ё", "Е").replace("ё", "е").casefold()


def normalized_choice_name(value: str) -> str:
    """Normalize ballot numbering, quotation, and dash typography for party joins."""
    value = BALLOT_POSITION_RE.sub("", normalized_text(value))
    value = value.replace("«", '"').replace("»", '"').replace("–", "-").replace("—", "-")
    return value.strip(' "').casefold()


class RegistryTableParser(HTMLParser):
    """Collect text, links, and hidden identities from every non-nested HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict[str, Any]]]] = []
        self.table: list[list[dict[str, Any]]] | None = None
        self.table_depth = 0
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        folded = tag.casefold()
        attributes = dict(attrs)
        if folded == "table":
            if self.table is None:
                self.table = []
                self.table_depth = 1
            else:
                self.table_depth += 1
            return
        if self.table is None:
            return
        if folded == "tr":
            self.row = []
        elif folded in ("td", "th") and self.row is not None:
            self.cell = {"text": [], "hrefs": [], "inputs": []}
        elif folded == "a" and self.cell is not None and attributes.get("href"):
            self.cell["hrefs"].append(str(attributes["href"]))
        elif folded == "input" and self.cell is not None:
            self.cell["inputs"].append(
                {
                    key: str(value)
                    for key, value in attributes.items()
                    if value is not None
                }
            )
        elif folded == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if self.table is None:
            return
        if folded in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = normalized_text("".join(self.cell["text"]))
            self.row.append(self.cell)
            self.cell = None
        elif folded == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif folded == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.tables.append(self.table)
                self.table = None


def registry_rows(payload: bytes) -> tuple[list[list[dict[str, Any]]], str]:
    source, encoding = decode_text(payload)
    parser = RegistryTableParser()
    parser.feed(source)
    return [row for table in parser.tables for row in table], encoding


def _candidate_identity(row: list[dict[str, Any]], source_url: str) -> tuple[int, str] | None:
    for index, cell in enumerate(row):
        for href in cell["hrefs"]:
            # Historical pages sometimes leave raw ampersands in attributes.
            url = urllib.parse.urljoin(source_url, href.replace("&amp;", "&"))
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if query.get("type") == "341" and query.get("vibid"):
                return index, str(query["vibid"])
    return None


def parse_candidate_registry(
    payload: bytes,
    source_url: str,
    *,
    election_vrn: str,
    scope: str,
) -> dict[str, Any]:
    """Parse official GAS type 220/221 candidate registries from 2003 onward."""
    rows, encoding = registry_rows(payload)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        identity = _candidate_identity(row, source_url)
        if identity is None:
            continue
        name_index, candidate_vibid = identity
        values = [str(cell["text"]) for cell in row]
        if name_index >= len(values) or not values[name_index]:
            continue
        modern = bool(values and values[0].isdigit())
        if modern:
            if scope == "district" and len(values) >= 10 and values[4].isdigit():
                district_number: int | None = int(values[4])
                birth_date = values[2]
                nominating_entity = values[3]
                nomination_status = values[5]
                registration_status = values[6]
                election_status = values[9]
            elif scope == "election" and len(values) >= 9:
                district_number = None
                birth_date = values[2]
                nominating_entity = values[3]
                nomination_status = values[6]
                registration_status = values[7]
                election_status = values[8]
            elif scope == "election" and len(values) >= 7:
                # Regional executive elections use the same type=221 registry,
                # but omit the federal-only columns between the nominator and
                # the three status fields.
                district_number = None
                birth_date = values[2]
                nominating_entity = values[3]
                nomination_status = values[4]
                registration_status = values[5]
                election_status = values[6]
            else:
                continue
        elif len(values) >= 10:
            district_match = DISTRICT_NUMBER_RE.search(values[2])
            district_number = int(district_match.group(1)) if district_match else None
            if scope == "district" and district_number is None:
                continue
            if scope == "election":
                district_number = None
            birth_date = ""
            nominating_entity = values[1]
            nomination_status = values[3]
            registration_status = values[5]
            election_status = values[9]
        else:
            continue
        candidates.append(
            {
                "candidate_vibid": candidate_vibid,
                "full_name": normalized_text(values[name_index]),
                "birth_date": normalized_text(birth_date),
                "nominating_entity": normalized_text(nominating_entity),
                "nomination_status": normalized_text(nomination_status),
                "registration_status": normalized_text(registration_status),
                "registry_election_status": normalized_text(election_status),
                "district_number": district_number,
                "is_elected": "избр" in election_status.casefold(),
            }
        )
    identities = [item["candidate_vibid"] for item in candidates]
    return {
        "valid_candidate_registry": bool(candidates),
        "encoding": encoding,
        "election_vrn_matches": election_vrn.encode() in payload
        or election_vrn in decode_text(payload)[0],
        "candidate_count": len(candidates),
        "unique_candidate_vibid_count": len(set(identities)),
        "duplicate_candidate_vibids": sorted(
            {
                identity
                for identity in identities
                if identities.count(identity) > 1
            }
        ),
        "district_numbers": sorted(
            {
                int(item["district_number"])
                for item in candidates
                if item["district_number"] is not None
            }
        ),
        "candidates": candidates,
    }


def parse_party_registry(
    payload: bytes, source_url: str, *, election_vrn: str
) -> dict[str, Any]:
    """Parse type 236 party/list identities, including the legacy 2003 shape."""
    rows, encoding = registry_rows(payload)
    parties: list[dict[str, Any]] = []
    for row in rows:
        values = [str(cell["text"]) for cell in row]
        if not values:
            continue
        vrnio = next(
            (
                str(item["value"])
                for cell in row
                for item in cell["inputs"]
                if item.get("name") == "vrnio" and item.get("value")
            ),
            None,
        )
        name_index = 1 if vrnio and values[0].isdigit() else 0
        identity_kind = "vrnio"
        detail_url = ""
        if vrnio is None:
            for href in row[name_index]["hrefs"] if name_index < len(row) else []:
                url = urllib.parse.urljoin(source_url, href.replace("&amp;", "&"))
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                if query.get("type") == "303" and query.get("vibid"):
                    vrnio = str(query["vibid"])
                    identity_kind = "vibid"
                    detail_url = url
                    break
        if not vrnio or name_index >= len(values) or not values[name_index]:
            continue
        modern = name_index == 1
        parties.append(
            {
                "party_list_vrnio": vrnio,
                "identity_kind": identity_kind,
                "detail_url": detail_url,
                "name": normalized_text(values[name_index]).strip(),
                "entity_kind": values[1] if not modern and len(values) > 1 else "",
                "federal_list_certification_date": values[2]
                if modern and len(values) > 2
                else "",
                "federal_list_certification_resolution": values[3]
                if modern and len(values) > 3
                else "",
                "federal_list_registration_date": values[4]
                if modern and len(values) > 4
                else "",
                "federal_list_registration_resolution": values[5]
                if modern and len(values) > 5
                else "",
            }
        )
    identities = [item["party_list_vrnio"] for item in parties]
    return {
        "valid_party_registry": bool(parties)
        and len(identities) == len(set(identities)),
        "encoding": encoding,
        "election_vrn_matches": election_vrn.encode() in payload
        or election_vrn in decode_text(payload)[0],
        "party_count": len(parties),
        "parties": parties,
    }


def parse_legacy_party_detail(
    payload: bytes, source_url: str, *, election_vrn: str
) -> dict[str, Any]:
    """Parse a 2003 type 303 association page and its type 321 list identity."""
    rows, encoding = registry_rows(payload)
    fields: dict[str, str] = {}
    list_vibid = ""
    for row in rows:
        if len(row) != 2:
            continue
        label = normalized_text(str(row[0]["text"]))
        value = normalized_text(str(row[1]["text"]))
        if label:
            fields[label] = value
        for href in row[1]["hrefs"]:
            url = urllib.parse.urljoin(source_url, href.replace("&amp;", "&"))
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            if query.get("type") == "321" and query.get("vibid"):
                list_vibid = str(query["vibid"])
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(source_url).query))
    association_vibid = str(query.get("vibid") or "")

    def integer(label: str) -> int | None:
        value = fields.get(label, "")
        return int(value) if value.isdigit() else None

    draw_number = integer("Номер жеребьевки")
    mandates = integer("Количество мандатов по партийному списку")
    votes = integer("Количество голосов 'За'")
    percent_text = fields.get("Процент голосов 'За'", "")
    try:
        vote_percent = float(percent_text.replace(",", "."))
    except ValueError:
        vote_percent = None
    result = {
        "association_vibid": association_vibid,
        "list_vibid": list_vibid,
        "official_name": fields.get("Наименование", ""),
        "entity_kind": fields.get("Вид", ""),
        "charter_registration_date": fields.get(
            "Дата регистрации Устава в Минюсте России", ""
        ),
        "charter_registration_number": fields.get(
            "Регистрационный номер в реестре Минюста России", ""
        ),
        "draw_number": draw_number,
        "mandates": mandates,
        "votes": votes,
        "vote_percent": vote_percent,
    }
    return {
        "valid_party_detail": bool(
            election_vrn in decode_text(payload)[0]
            and association_vibid
            and list_vibid
            and result["official_name"]
        ),
        "encoding": encoding,
        **result,
    }


def registry_source(observation: dict[str, Any]) -> dict[str, Any]:
    result = {
        "official_url": observation["requested_url"],
        "sha256": observation["sha256"],
        "report_type": int(observation["report_type"]),
    }
    for key in ("final_url", "retrieved_at", "provenance"):
        if observation.get(key) is not None:
            result[key] = observation[key]
    return result
