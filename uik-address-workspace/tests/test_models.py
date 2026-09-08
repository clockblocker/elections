from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from uik_address.io import read_jsonl, write_jsonl
from uik_address.models import BackboneRow, CommissionContact, canonical_region_code


class ModelTests(unittest.TestCase):
    def test_canonical_region_code_preserves_integer_zero(self) -> None:
        self.assertEqual("0", canonical_region_code(0))
        self.assertEqual("0", canonical_region_code("000"))
        self.assertEqual("77", canonical_region_code(" 077 "))
        self.assertEqual("", canonical_region_code(None))

    def test_models_accept_integer_zero_subject_code(self) -> None:
        source = {"url": "https://official.test", "status": 200}
        backbone = BackboneRow.from_dict(
            {
                "subject_code": 0,
                "tik_number": 99,
                "uik_number": 8000,
                "source": source,
            }
        )
        contact = CommissionContact.from_dict(
            {
                "subject_code": 0,
                "commission_type": "uik",
                "commission_number": 8000,
                "source": source,
            }
        )
        self.assertEqual("0", backbone.subject_code)
        self.assertEqual("0", contact.subject_code)


class IoTests(unittest.TestCase):
    def test_failed_jsonl_write_preserves_previous_file(self) -> None:
        def broken_rows():
            yield {"new": 1}
            raise RuntimeError("stopped")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            path.write_text('{"old": true}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                write_jsonl(path, broken_rows())
            self.assertEqual('{"old": true}\n', path.read_text(encoding="utf-8"))
            self.assertEqual([], list(path.parent.glob(".*.part")))

    def test_jsonl_parse_error_includes_physical_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl"
            path.write_text('\n{"ok": true}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"rows\.jsonl:3: invalid JSON"):
                list(read_jsonl(path))


if __name__ == "__main__":
    unittest.main()
