from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from house_polling_address_map.cli import main


class CliTest(unittest.TestCase):
    def test_init_and_empty_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = [
                "--database",
                str(root / "map.sqlite3"),
                "--raw-dir",
                str(root / "raw"),
            ]
            with patch("builtins.print"):
                self.assertEqual(0, main([*arguments, "init"]))
            with patch("builtins.print") as output:
                self.assertEqual(0, main([*arguments, "coverage"]))

            report = json.loads(output.call_args.args[0])
            self.assertEqual(0, report["total"])
            self.assertEqual(0, report["pending"])


if __name__ == "__main__":
    unittest.main()
