"""Streaming reader for the official Russian GAR/FIAS XML exports.

GAR distributes address objects, hierarchy links, house-number types, and
houses in separate XML files inside ZIP archives.  :class:`GarIndex` indexes
only the comparatively small address hierarchy in SQLite and leaves houses in
the archive.  Consequently ``iter_buildings`` is a streaming operation and
does not make memory use proportional to the number of buildings.

The reader intentionally understands the stable, public GAR attributes rather
than a particular export date.  Unknown XML files and attributes are ignored.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Self
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

Hierarchy = Literal["administrative", "municipal"]
_PART_SEPARATOR = "\x1f"
_SPACE_RE = re.compile(r"\s+")
_NUMBER_SPACE_RE = re.compile(r"(?<=\d)\s+(?=[A-Za-zА-Яа-яЁё](?:\b|$))")
_SEPARATOR_SPACE_RE = re.compile(r"\s*([/-])\s*")


class GarFormatError(ValueError):
    """Raised when a selected GAR XML member cannot be parsed."""


@dataclass(frozen=True, slots=True)
class GarBuilding:
    """One current building address from GAR.

    ``gar_id`` is stable across exports: it is based on GAR's OBJECTGUID when
    present and on a deterministic digest of the canonical record otherwise.
    Apartments and rooms are deliberately outside this representation.
    """

    gar_id: str
    object_id: int
    object_guid: str | None
    region_code: str | None
    parent_object_id: int | None
    address_parts: tuple[str, ...]
    house_number: str | None
    house_type: str | None
    corpus: str | None
    structure: str | None
    additional_numbers: tuple[tuple[str, str], ...]
    address: str
    source_member: str


def normalize_number(value: str | None) -> str | None:
    """Normalize a GAR house/corpus/structure number without changing meaning."""

    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = _SPACE_RE.sub(" ", value).strip()
    if not value:
        return None
    value = _SEPARATOR_SPACE_RE.sub(r"\1", value)
    value = _NUMBER_SPACE_RE.sub("", value)
    return value.upper()


class GarIndex:
    """SQLite-backed index over a GAR XML ZIP export.

    Args:
        source: A GAR ZIP, an XML file, or a directory containing ZIP/XML files.
            ZIPs embedded in another ZIP are supported and are spooled to disk.
        database: Optional reusable SQLite path.  When omitted, a temporary
            on-disk database is created and removed by :meth:`close`.
        hierarchy: Select GAR's administrative or municipal hierarchy.
    """

    def __init__(
        self,
        source: str | os.PathLike[str],
        *,
        database: str | os.PathLike[str] | None = None,
        hierarchy: Hierarchy = "administrative",
    ) -> None:
        if hierarchy not in {"administrative", "municipal"}:
            raise ValueError("hierarchy must be 'administrative' or 'municipal'")
        self.source = Path(source)
        self.hierarchy = hierarchy
        self._owns_database = database is None
        if database is None:
            descriptor, filename = tempfile.mkstemp(prefix="gar-index-", suffix=".sqlite3")
            os.close(descriptor)
            self.database = Path(filename)
        else:
            self.database = Path(database)
        self._connection: sqlite3.Connection | None = None
        self._built = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._owns_database:
            self.database.unlink(missing_ok=True)

    def build(self) -> None:
        """(Re)build the address-object and hierarchy index."""

        connection = self._connect()
        connection.executescript(
            """
            DROP TABLE IF EXISTS address_objects;
            DROP TABLE IF EXISTS hierarchy;
            DROP TABLE IF EXISTS number_types;
            DROP TABLE IF EXISTS resolved_addresses;
            CREATE TABLE address_objects (
                object_id INTEGER PRIMARY KEY,
                object_guid TEXT,
                name TEXT NOT NULL,
                type_name TEXT NOT NULL,
                component TEXT NOT NULL
            );
            CREATE TABLE hierarchy (
                object_id INTEGER PRIMARY KEY,
                parent_object_id INTEGER,
                region_code TEXT
            );
            CREATE TABLE number_types (
                type_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL
            );
            CREATE TABLE resolved_addresses (
                object_id INTEGER PRIMARY KEY,
                region_code TEXT,
                address TEXT NOT NULL,
                parts TEXT NOT NULL
            );
            """
        )

        hierarchy_kind = "adm_hierarchy" if self.hierarchy == "administrative" else "mun_hierarchy"
        for member_name, stream in _iter_xml_members(self.source):
            kind = _member_kind(member_name)
            if kind == "address_objects":
                self._load_address_objects(connection, member_name, stream)
            elif kind == hierarchy_kind:
                self._load_hierarchy(connection, member_name, stream)
            elif kind == "number_types":
                self._load_number_types(connection, member_name, stream)

        connection.execute("CREATE INDEX hierarchy_parent_idx ON hierarchy(parent_object_id)")
        self._resolve_addresses(connection)
        connection.commit()
        self._built = True

    def iter_buildings(
        self,
        *,
        region_codes: Iterable[str] | None = None,
        house_type_names: Iterable[str] | None = None,
        include_without_number: bool = False,
    ) -> Iterator[GarBuilding]:
        """Yield current building addresses in deterministic archive order.

        ``region_codes`` and ``house_type_names`` are optional exact filters.
        House type matching is Unicode-normalized and case-insensitive.  Only
        ``AS_HOUSES`` records with current/active flags are considered; GAR
        apartment and room files are never read.
        """

        if not self._built:
            self.build()
        connection = self._connect()
        wanted_regions = (
            {_normalize_region_code(code) for code in region_codes}
            if region_codes is not None
            else None
        )
        wanted_types = (
            {_comparison_key(value) for value in house_type_names}
            if house_type_names is not None
            else None
        )
        type_map = {
            row[0]: (row[1], row[2])
            for row in connection.execute("SELECT type_id, name, short_name FROM number_types")
        }
        lookup_parent = connection.cursor()

        for member_name, stream in _iter_xml_members(self.source):
            if _member_kind(member_name) != "houses":
                continue
            for attributes in _iter_rows(stream, member_name, "OBJECTID"):
                if not _is_current(attributes):
                    continue
                try:
                    object_id = int(attributes["OBJECTID"])
                except (KeyError, ValueError):
                    continue
                house_number = normalize_number(attributes.get("HOUSENUM"))
                add_number_1 = normalize_number(attributes.get("ADDNUM1"))
                add_number_2 = normalize_number(attributes.get("ADDNUM2"))
                if not include_without_number and not (
                    house_number or add_number_1 or add_number_2
                ):
                    continue

                hierarchy_row = lookup_parent.execute(
                    """
                    SELECT h.parent_object_id,
                           COALESCE(h.region_code, r.region_code),
                           r.address,
                           r.parts
                      FROM hierarchy h
                 LEFT JOIN resolved_addresses r
                        ON r.object_id = h.parent_object_id
                     WHERE h.object_id = ?
                    """,
                    (object_id,),
                ).fetchone()
                if hierarchy_row is None:
                    # An address without its hierarchy cannot be mapped
                    # unambiguously and is therefore not canonical.
                    continue
                parent_object_id, region_code, parent_address, serialized_parts = hierarchy_row
                region_code = _normalize_region_code(region_code)
                if wanted_regions is not None and region_code not in wanted_regions:
                    continue
                if parent_address is None:
                    continue

                house_type = _number_type(type_map, attributes.get("HOUSETYPE"), "д")
                if wanted_types is not None and _comparison_key(house_type) not in wanted_types:
                    continue
                corpus: str | None = None
                structure: str | None = None
                extras: list[tuple[str, str]] = []
                displayed: list[str] = []
                if house_number:
                    displayed.append(_display_number(house_type, house_number))

                for number, type_id in (
                    (add_number_1, attributes.get("ADDTYPE1")),
                    (add_number_2, attributes.get("ADDTYPE2")),
                ):
                    if not number:
                        continue
                    label = _number_type(type_map, type_id, "")
                    category = _number_category(label)
                    if category == "corpus" and corpus is None:
                        corpus = number
                    elif category == "structure" and structure is None:
                        structure = number
                    else:
                        extras.append((label, number))
                    displayed.append(_display_number(label, number))

                parts = tuple(serialized_parts.split(_PART_SEPARATOR))
                full_parts = parts + tuple(displayed)
                address = ", ".join(full_parts)
                object_guid = _normalize_guid(attributes.get("OBJECTGUID"))
                yield GarBuilding(
                    gar_id=_stable_id(
                        object_guid=object_guid,
                        object_id=object_id,
                        region_code=region_code,
                        address=address,
                    ),
                    object_id=object_id,
                    object_guid=object_guid,
                    region_code=region_code,
                    parent_object_id=parent_object_id,
                    address_parts=parts,
                    house_number=house_number,
                    house_type=house_type or None,
                    corpus=corpus,
                    structure=structure,
                    additional_numbers=tuple(extras),
                    address=address,
                    source_member=member_name,
                )

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.database)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA temp_store=FILE")
        return self._connection

    @staticmethod
    def _load_address_objects(
        connection: sqlite3.Connection, member_name: str, stream: BinaryIO
    ) -> None:
        rows: list[tuple[int, str | None, str, str, str]] = []
        for attributes in _iter_rows(stream, member_name, "OBJECTID"):
            if not _is_current(attributes):
                continue
            try:
                object_id = int(attributes["OBJECTID"])
            except (KeyError, ValueError):
                continue
            name = _clean_text(attributes.get("NAME"))
            type_name = _clean_text(attributes.get("TYPENAME"))
            if not name:
                continue
            component = " ".join(value for value in (type_name, name) if value)
            rows.append(
                (
                    object_id,
                    _normalize_guid(attributes.get("OBJECTGUID")),
                    name,
                    type_name,
                    component,
                )
            )
            if len(rows) >= 10_000:
                connection.executemany(
                    "INSERT OR REPLACE INTO address_objects VALUES (?, ?, ?, ?, ?)", rows
                )
                rows.clear()
        connection.executemany(
            "INSERT OR REPLACE INTO address_objects VALUES (?, ?, ?, ?, ?)", rows
        )

    @staticmethod
    def _load_hierarchy(connection: sqlite3.Connection, member_name: str, stream: BinaryIO) -> None:
        rows: list[tuple[int, int | None, str | None]] = []
        for attributes in _iter_rows(stream, member_name, "OBJECTID"):
            if not _is_current(attributes):
                continue
            try:
                object_id = int(attributes["OBJECTID"])
            except (KeyError, ValueError):
                continue
            try:
                raw_parent = int(attributes.get("PARENTOBJID", "0"))
            except ValueError:
                raw_parent = 0
            rows.append(
                (
                    object_id,
                    raw_parent or None,
                    _normalize_region_code(attributes.get("REGIONCODE")),
                )
            )
            if len(rows) >= 10_000:
                connection.executemany("INSERT OR REPLACE INTO hierarchy VALUES (?, ?, ?)", rows)
                rows.clear()
        connection.executemany("INSERT OR REPLACE INTO hierarchy VALUES (?, ?, ?)", rows)

    @staticmethod
    def _load_number_types(
        connection: sqlite3.Connection, member_name: str, stream: BinaryIO
    ) -> None:
        rows: list[tuple[int, str, str]] = []
        for attributes in _iter_rows(stream, member_name, "ID"):
            if not _is_active(attributes):
                continue
            try:
                type_id = int(attributes["ID"])
            except (KeyError, ValueError):
                continue
            name = _clean_text(attributes.get("NAME"))
            short_name = _clean_text(attributes.get("SHORTNAME")) or name
            rows.append((type_id, name, short_name))
        connection.executemany("INSERT OR REPLACE INTO number_types VALUES (?, ?, ?)", rows)

    @staticmethod
    def _resolve_addresses(connection: sqlite3.Connection) -> None:
        # A missing parent marks a root as well.  This makes regional extracts
        # useful even when their federal ancestors were not included.
        connection.execute(
            """
            INSERT OR IGNORE INTO resolved_addresses(object_id, region_code, address, parts)
            SELECT o.object_id, h.region_code, o.component, o.component
              FROM address_objects o
         LEFT JOIN hierarchy h ON h.object_id = o.object_id
         LEFT JOIN address_objects parent ON parent.object_id = h.parent_object_id
             WHERE h.parent_object_id IS NULL OR parent.object_id IS NULL
            """
        )
        while True:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO resolved_addresses(object_id, region_code, address, parts)
                SELECT child.object_id,
                       COALESCE(h.region_code, parent.region_code),
                       parent.address || ', ' || child.component,
                       parent.parts || ? || child.component
                  FROM address_objects child
                  JOIN hierarchy h ON h.object_id = child.object_id
                  JOIN resolved_addresses parent
                    ON parent.object_id = h.parent_object_id
                """,
                (_PART_SEPARATOR,),
            )
            if cursor.rowcount == 0:
                break


def iter_gar_buildings(
    source: str | os.PathLike[str],
    *,
    database: str | os.PathLike[str] | None = None,
    hierarchy: Hierarchy = "administrative",
    region_codes: Iterable[str] | None = None,
    house_type_names: Iterable[str] | None = None,
    include_without_number: bool = False,
) -> Iterator[GarBuilding]:
    """Convenience wrapper that owns and cleans up a :class:`GarIndex`."""

    with GarIndex(source, database=database, hierarchy=hierarchy) as index:
        index.build()
        yield from index.iter_buildings(
            region_codes=region_codes,
            house_type_names=house_type_names,
            include_without_number=include_without_number,
        )


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return _SPACE_RE.sub(" ", value).strip()


def _comparison_key(value: str) -> str:
    return _clean_text(value).rstrip(".").casefold()


def _normalize_region_code(value: object) -> str | None:
    if value is None:
        return None
    cleaned = _clean_text(str(value))
    if not cleaned:
        return None
    return cleaned.zfill(2) if cleaned.isdigit() and len(cleaned) < 2 else cleaned


def _normalize_guid(value: str | None) -> str | None:
    cleaned = _clean_text(value).strip("{}").lower()
    return cleaned or None


def _stable_id(
    *, object_guid: str | None, object_id: int, region_code: str | None, address: str
) -> str:
    if object_guid:
        return f"gar:{object_guid}"
    payload = f"{region_code or ''}\x1f{object_id}\x1f{address.casefold()}".encode()
    return f"gar:sha256:{hashlib.sha256(payload).hexdigest()}"


def _number_type(type_map: dict[int, tuple[str, str]], type_id: str | None, fallback: str) -> str:
    try:
        names = type_map.get(int(type_id or ""))
    except ValueError:
        names = None
    if names is None:
        return fallback
    name, short_name = names
    return _clean_text(short_name or name).rstrip(".")


def _number_category(label: str) -> str:
    key = _comparison_key(label)
    if "корп" in key:
        return "corpus"
    if "стр" in key or "строен" in key or "сооруж" in key:
        return "structure"
    return "additional"


def _display_number(label: str, number: str) -> str:
    return f"{label} {number}".strip()


def _is_active(attributes: dict[str, str]) -> bool:
    return attributes.get("ISACTIVE", "1") == "1"


def _is_current(attributes: dict[str, str]) -> bool:
    return _is_active(attributes) and attributes.get("ISACTUAL", "1") == "1"


def _member_kind(member_name: str) -> str | None:
    basename = Path(member_name).name.upper()
    if not basename.endswith(".XML"):
        return None
    if "ADDHOUSE_TYPES" in basename:
        return "number_types"
    if "AS_ADM_HIERARCHY_" in basename:
        return "adm_hierarchy"
    if "AS_MUN_HIERARCHY_" in basename:
        return "mun_hierarchy"
    if "AS_ADDR_OBJ_" in basename and not any(
        marker in basename for marker in ("PARAMS", "TYPES", "DIVISION")
    ):
        return "address_objects"
    if "AS_HOUSES_" in basename and "PARAMS" not in basename:
        return "houses"
    return None


def _iter_rows(
    stream: BinaryIO, member_name: str, required_attribute: str
) -> Iterator[dict[str, str]]:
    try:
        for _event, element in ElementTree.iterparse(stream, events=("end",)):
            if required_attribute in element.attrib:
                yield dict(element.attrib)
            element.clear()
    except ElementTree.ParseError as error:
        raise GarFormatError(f"invalid GAR XML member {member_name}: {error}") from error


@contextmanager
def _opened_file(path: Path) -> Iterator[BinaryIO]:
    with path.open("rb") as stream:
        yield stream


def _iter_xml_members(source: Path) -> Iterator[tuple[str, BinaryIO]]:
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.casefold() in {".xml", ".zip"}:
                yield from _iter_xml_members(path)
        return
    if not source.exists():
        raise FileNotFoundError(source)
    if source.suffix.casefold() == ".xml":
        with _opened_file(source) as stream:
            yield source.name, stream
        return
    if source.suffix.casefold() != ".zip":
        raise ValueError(f"GAR source must be a ZIP, XML, or directory: {source}")

    try:
        with ZipFile(source) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue
                suffix = Path(info.filename).suffix.casefold()
                if suffix == ".xml":
                    with archive.open(info) as stream:
                        yield info.filename, stream
                elif suffix == ".zip":
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix="gar-nested-", suffix=".zip"
                    )
                    try:
                        with (
                            os.fdopen(descriptor, "wb") as destination,
                            archive.open(info) as nested,
                        ):
                            shutil.copyfileobj(nested, destination, length=1024 * 1024)
                        for member_name, stream in _iter_xml_members(Path(temporary_name)):
                            yield f"{info.filename}!/{member_name}", stream
                    finally:
                        Path(temporary_name).unlink(missing_ok=True)
    except BadZipFile as error:
        raise GarFormatError(f"invalid GAR ZIP archive {source}: {error}") from error


__all__ = [
    "GarBuilding",
    "GarFormatError",
    "GarIndex",
    "iter_gar_buildings",
    "normalize_number",
]
