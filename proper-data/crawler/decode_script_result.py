from __future__ import annotations

import argparse
import ast
import collections
import json
import re
from pathlib import Path
from typing import Any

from lxml import html as lxml_html

try:
    from .common import decode_text, json_write
except ImportError:  # Direct script execution.
    from common import decode_text, json_write


FUNCTION_START = re.compile(r"\bvar\s+(\w+)\s*=\s*function\s*\(")
ARGUMENT = re.compile(r"'(?:\\.|[^'])*'|-?\d+|false|true|[A-Za-z_]\w*")


def _functions(script: str) -> dict[str, str]:
    starts = list(FUNCTION_START.finditer(script))
    return {
        match.group(1): script[
            match.start() : starts[index + 1].start() if index + 1 < len(starts) else len(script)
        ]
        for index, match in enumerate(starts)
    }


def _semantics(script: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, body in _functions(script).items():
        if "charAt(" in body:
            result[name] = "insert"
        elif "style.position = 'absolute'" in body:
            result[name] = "overlay"
        elif "getElementsByTagName('td')" in body and body.count(".innerHTML =") >= 2:
            result[name] = "swap"
        elif ".innerHTML.split('')" in body and ".splice(" in body:
            result[name] = "remove"
        elif "getElementsByClassName" in body and ".innerHTML =" in body:
            result[name] = "replace"
    return result


def _arguments(source: str) -> list[Any]:
    result: list[Any] = []
    for token in ARGUMENT.findall(source):
        if token.startswith("'"):
            result.append(ast.literal_eval(token))
        elif token in {"false", "true"}:
            result.append(token == "true")
        elif re.fullmatch(r"-?\d+", token):
            result.append(int(token))
        else:
            result.append(token)
    return result


def _leaf(element: Any) -> Any:
    while len(element):
        element = element[-1]
    return element


def _text(element: Any) -> str:
    return "".join(element.itertext())


def _put(element: Any, value: str) -> None:
    for child in list(element):
        element.remove(child)
    element.text = value


def decode_script_tables(source: str) -> list[list[list[str]]]:
    document = lxml_html.fromstring(source)
    scripts = [element.text or "" for element in document.xpath("//script")]
    decoded: list[list[list[str]]] = []
    for table in document.xpath('//table[contains(concat(" ", @class, " "), " table-striped ")]'):
        classes = (table.get("class") or "").split()
        if not classes:
            continue
        table_name = classes[-1]
        script = next((value for value in scripts if f"var {table_name} =" in value), None)
        if script is None:
            continue
        semantics = _semantics(script)
        if not semantics:
            continue
        main_start = script.find("var a = function")
        main = script[max(main_start, 0) :]
        resize = main.find("window.addEventListener")
        if resize >= 0:
            main = main[:resize]
        call = re.compile(
            r"\b(" + "|".join(map(re.escape, semantics)) + r")\((.*?)\)\s*;"
        )
        cells = table.xpath(".//td")

        def by_class(name: str, current_table: Any = table) -> list[Any]:
            return current_table.xpath(
                './/*[contains(concat(" ", normalize-space(@class), " "), $value)]',
                value=f" {name} ",
            )

        for match in call.finditer(main):
            operation = semantics[match.group(1)]
            arguments = _arguments(match.group(2))
            if operation == "replace":
                class_name, value, _ = arguments
                for element in by_class(class_name):
                    _put(element, value)
            elif operation == "remove":
                class_name, index, _ = arguments
                for element in by_class(class_name):
                    element = _leaf(element)
                    value = _text(element)
                    changed = value[:index] if index < 0 else value[:index] + value[index + 1 :]
                    _put(element, changed)
            elif operation == "swap":
                left, right, _ = arguments
                first, second = _leaf(cells[int(left)]), _leaf(cells[int(right)])
                first_value, second_value = _text(first), _text(second)
                _put(first, second_value)
                _put(second, first_value)
            elif operation == "insert":
                char_index, source, destination_index, destination, dot_index, _ = arguments
                source_element = _leaf(cells[source])
                target = _leaf(cells[destination])
                value = _text(target)
                if dot_index is not False:
                    value = value[:dot_index] + "." + value[dot_index:]
                value = (
                    value[:destination_index]
                    + _text(source_element).strip()[char_index]
                    + value[destination_index:]
                )
                _put(target, value)
            elif operation == "overlay":
                class_name, destination, _ = arguments
                overlays = by_class(class_name)
                if overlays:
                    _put(_leaf(cells[destination]), _text(_leaf(overlays[0])))

        rows = [
            [" ".join(cell.text_content().split()) for cell in row.xpath("./th|./td")]
            for row in table.xpath(".//tr")
        ]
        decoded.append(rows)
    return decoded


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply GAS's inline JavaScript table decoder")
    parser.add_argument("html", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, encoding = decode_text(args.html.read_bytes())
    tables = decode_script_tables(source)
    uik_tables = [
        table for table in tables if any("УИК" in cell for row in table for cell in row)
    ]
    rows = max(
        uik_tables or tables,
        key=lambda value: max(map(len, value), default=0),
        default=[],
    )
    widths = collections.Counter(map(len, rows))
    maximum = max(widths, key=lambda width: (widths[width], width), default=0)
    rows = [row for row in rows if len(row) == maximum]
    result = {
        "source_html": str(args.html),
        "source_encoding": encoding,
        "row_count": len(rows),
        "max_columns": maximum,
        "rows": rows,
    }
    json_write(args.output, result)
    print(json.dumps({"output": str(args.output), "rows": len(rows), "columns": maximum}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
