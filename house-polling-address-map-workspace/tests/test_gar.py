from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from house_polling_address_map.gar import (
    GarFormatError,
    GarIndex,
    iter_gar_buildings,
    normalize_number,
)


def _xml(tag: str, rows: list[dict[str, object]]) -> str:
    items = "".join(
        "<OBJECT " + " ".join(f'{key}="{value}"' for key, value in row.items()) + "/>"
        for row in rows
    )
    return f'<?xml version="1.0" encoding="utf-8"?><{tag}>{items}</{tag}>'


def _write_gar_fixture(path: Path, *, nested: bool = False) -> Path:
    members = {
        "AS_ADDR_OBJ_20260908.XML": _xml(
            "ADDRESSOBJECTS",
            [
                {
                    "OBJECTID": 1,
                    "OBJECTGUID": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                    "NAME": "Новосибирская",
                    "TYPENAME": "обл",
                    "ISACTUAL": 1,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 10,
                    "OBJECTGUID": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
                    "NAME": "Новосибирск",
                    "TYPENAME": "г",
                    "ISACTUAL": 1,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 100,
                    "OBJECTGUID": "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC",
                    "NAME": "Красный проспект",
                    "TYPENAME": "пр-кт",
                    "ISACTUAL": 1,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 101,
                    "OBJECTGUID": "DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD",
                    "NAME": "Старая",
                    "TYPENAME": "ул",
                    "ISACTUAL": 0,
                    "ISACTIVE": 1,
                },
            ],
        ),
        "AS_ADM_HIERARCHY_20260908.XML": _xml(
            "ITEMS",
            [
                {"OBJECTID": 1, "REGIONCODE": 54, "ISACTIVE": 1},
                {
                    "OBJECTID": 10,
                    "PARENTOBJID": 1,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 100,
                    "PARENTOBJID": 10,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1000,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1001,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1002,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
            ],
        ),
        "AS_MUN_HIERARCHY_20260908.XML": _xml(
            "ITEMS",
            [
                {"OBJECTID": 1, "REGIONCODE": 54, "ISACTIVE": 1},
                {
                    "OBJECTID": 100,
                    "PARENTOBJID": 1,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1000,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1001,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1002,
                    "PARENTOBJID": 100,
                    "REGIONCODE": 54,
                    "ISACTIVE": 1,
                },
            ],
        ),
        "ADDHOUSE_TYPES_20260908.XML": _xml(
            "HOUSETYPES",
            [
                {"ID": 2, "NAME": "Дом", "SHORTNAME": "д.", "ISACTIVE": 1},
                {"ID": 4, "NAME": "Корпус", "SHORTNAME": "корп.", "ISACTIVE": 1},
                {
                    "ID": 5,
                    "NAME": "Строение",
                    "SHORTNAME": "стр.",
                    "ISACTIVE": 1,
                },
            ],
        ),
        "AS_HOUSES_20260908.XML": _xml(
            "HOUSES",
            [
                {
                    "OBJECTID": 1000,
                    "OBJECTGUID": "{EEEEEEEE-EEEE-EEEE-EEEE-EEEEEEEEEEEE}",
                    "HOUSENUM": " 12 а ",
                    "HOUSETYPE": 2,
                    "ADDNUM1": " 2 ",
                    "ADDTYPE1": 4,
                    "ADDNUM2": "  3 / 1 ",
                    "ADDTYPE2": 5,
                    "ISACTUAL": 1,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1001,
                    "OBJECTGUID": "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
                    "HOUSENUM": 13,
                    "HOUSETYPE": 2,
                    "ISACTUAL": 0,
                    "ISACTIVE": 1,
                },
                {
                    "OBJECTID": 1002,
                    "OBJECTGUID": "11111111-1111-1111-1111-111111111111",
                    "HOUSETYPE": 2,
                    "ISACTUAL": 1,
                    "ISACTIVE": 1,
                },
            ],
        ),
        # Must not be mistaken for an address-object or house data file.
        "AS_ADDR_OBJ_PARAMS_20260908.XML": _xml(
            "PARAMS", [{"OBJECTID": 999, "NAME": "not an address"}]
        ),
        "AS_ROOMS_20260908.XML": _xml("ROOMS", [{"OBJECTID": 2000, "NUMBER": 1, "ISACTIVE": 1}]),
    }
    if not nested:
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return path

    inner = path.with_name("region.zip")
    with ZipFile(inner, "w", ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.write(inner, "54.zip")
    inner.unlink()
    return path


class GarIngestionTest(unittest.TestCase):
    def test_streams_current_buildings_and_normalizes_number_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = _write_gar_fixture(Path(directory) / "gar.zip")
            buildings = list(iter_gar_buildings(archive))

        self.assertEqual(1, len(buildings))
        building = buildings[0]
        self.assertEqual("gar:eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", building.gar_id)
        self.assertEqual(1000, building.object_id)
        self.assertEqual("54", building.region_code)
        self.assertEqual(
            ("обл Новосибирская", "г Новосибирск", "пр-кт Красный проспект"),
            building.address_parts,
        )
        self.assertEqual("12А", building.house_number)
        self.assertEqual("д", building.house_type)
        self.assertEqual("2", building.corpus)
        self.assertEqual("3/1", building.structure)
        self.assertEqual((), building.additional_numbers)
        self.assertTrue(building.address.endswith("д 12А, корп 2, стр 3/1"))

    def test_filters_by_region_and_house_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = _write_gar_fixture(Path(directory) / "gar.zip")
            self.assertEqual([], list(iter_gar_buildings(archive, region_codes={"77"})))
            self.assertEqual(1, len(list(iter_gar_buildings(archive, region_codes={"54"}))))
            self.assertEqual(1, len(list(iter_gar_buildings(archive, house_type_names={"Д."}))))
            self.assertEqual([], list(iter_gar_buildings(archive, house_type_names={"владение"})))

    def test_can_select_municipal_hierarchy_and_read_nested_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = _write_gar_fixture(Path(directory) / "gar.zip", nested=True)
            building = next(iter_gar_buildings(archive, hierarchy="municipal"))

        self.assertEqual(("обл Новосибирская", "пр-кт Красный проспект"), building.address_parts)
        self.assertTrue(building.source_member.startswith("54.zip!/"))

    def test_explicit_index_database_is_reusable_and_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = _write_gar_fixture(root / "gar.zip")
            database = root / "index.sqlite3"
            with GarIndex(archive, database=database) as index:
                index.build()
                first = list(index.iter_buildings())
                second = list(index.iter_buildings())
            self.assertEqual(first, second)
            self.assertTrue(database.exists())

    def test_normalize_number(self) -> None:
        cases = [
            (" 12 а ", "12А"),
            ("3 / 1", "3/1"),
            ("  7\u00a0- б ", "7-Б"),
            ("  ", None),
            (None, None),
        ]
        for raw, normalized in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalized, normalize_number(raw))

    def test_invalid_selected_xml_names_the_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "gar.zip"
            with ZipFile(archive, "w") as destination:
                destination.writestr("AS_ADDR_OBJ_broken.XML", "<ADDRESSOBJECTS>")
            with self.assertRaisesRegex(GarFormatError, "AS_ADDR_OBJ_broken.XML"):
                list(iter_gar_buildings(archive))


if __name__ == "__main__":
    unittest.main()
