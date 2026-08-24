"""Undo the CSS/font/JavaScript obfuscation used on 2021 CEC result pages.

Adapted from Alexander Shpilkin's CC0 reference implementation:
https://gist.github.com/alexshpilkin/bf25962064e570d10aca9a8a4b325b78
"""

from __future__ import annotations

import re
from collections import namedtuple
from io import BytesIO

from fontTools.ttLib import TTFont
from lxml.etree import tostring
from lxml.html import document_fromstring

Style = namedtuple("Style", "visible scramble content", defaults=(True, False, None))
HIDE = re.compile(
    r"display: *none|(top|left): *-9+px|z-index: *-9+|"
    r"(font-size|opacity): *0|(width|height): *0(px)?|"
    r"color: *(white|transparent)|visibility: *hidden"
)
CONTENT = re.compile(r"content: *'([^']*)'")
FONTFAM = re.compile(r'font-family: *"([^"]*)"( *!important)?')
FONTURL = re.compile(r'src:.* url\("\./([^"]*\.ttf)"\).*')
SELECTOR = re.compile(r"\.([a-z_]*(::after)?)")


def font_url(html: str) -> str | None:
    """Return the relative TTF URL embedded in an obfuscated CEC page."""
    match = re.search(r'url\("\./([^"]*\.ttf)"\)', html)
    return match.group(1) if match else None


def deobfuscate_cec_html(html: str, font_bytes: bytes) -> str:
    """Return the visible result container using only preserved HTML and TTF bytes."""
    tree = document_fromstring(html)
    containers = tree.xpath('//*[contains(concat(" ", @class, " "), " show ")]')
    if len(containers) != 1:
        raise ValueError(f"expected one CEC result container, found {len(containers)}")
    container = containers[0]
    css_nodes = container.xpath(".//style")
    js_nodes = container.xpath(".//script")
    if len(css_nodes) != 1 or len(js_nodes) != 1:
        raise ValueError("CEC obfuscated page lacks its inline style or script")
    css_node, js_node = css_nodes[0], js_nodes[0]
    css = str(css_node.text or "")
    js = str(js_node.text or "")
    css_node.drop_tree()
    js_node.drop_tree()

    byclass: dict[str, list] = {}
    for node in container.xpath(".//*[@class]"):
        for cls in node.classes:
            byclass.setdefault(cls, []).append(node)

    font_family = None
    embedded_font_url = None

    def parse_style(declarations: str) -> Style:
        nonlocal font_family, embedded_font_url
        style = Style()
        for declaration in declarations.split(";"):
            declaration = declaration.strip()
            if HIDE.fullmatch(declaration):
                style = style._replace(visible=False)
            elif match := CONTENT.fullmatch(declaration):
                style = style._replace(content=match.group(1))
            elif match := FONTFAM.fullmatch(declaration):
                if font_family is None:
                    font_family = match.group(1)
                if match.group(1) != font_family:
                    raise ValueError("multiple scrambled font families")
                style = style._replace(scramble=True)
            elif match := FONTURL.fullmatch(declaration):
                if embedded_font_url is not None:
                    raise ValueError("multiple scrambled font URLs")
                embedded_font_url = match.group(1)
        return style

    styles: dict[str, Style | None] = {}
    afters: dict[str, Style] = {}
    end = 0
    for match in re.finditer(r" *([-@a-z_.: ]+?) *\{([^}]*)\}", css):
        if match.start() != end:
            raise ValueError("unrecognized CEC stylesheet content")
        end = match.end()
        selector, declarations = match.groups()
        style = parse_style(declarations)
        if selector == "@font-face":
            continue
        parent, selector = selector.split()
        if parent[0] != "." or len(byclass.get(parent[1:], ())) != 1:
            raise ValueError("unexpected CEC stylesheet parent selector")
        parsed = SELECTOR.fullmatch(selector)
        if parsed is None:
            raise ValueError("unexpected CEC stylesheet selector")
        name = parsed.group(1)
        if name.endswith("::after"):
            afters[name.removesuffix("::after")] = style
        else:
            styles[name] = style
    if css[end:].strip():
        raise ValueError("trailing unrecognized CEC stylesheet content")
    if embedded_font_url is None:
        raise ValueError("CEC stylesheet contains no scrambled font URL")

    ttf = TTFont(BytesIO(font_bytes))
    inverse = {name: codepoint for codepoint, name in ttf.getBestCmap().items()}
    names = "zero one two three four five six seven eight nine".split()
    try:
        substitution = {chr(inverse[name]): str(number) for number, name in enumerate(names)}
    except KeyError as exc:
        raise ValueError(f"CEC font lacks digit glyph {exc.args[0]}") from exc

    def unscramble(value: str) -> str:
        try:
            return "".join(substitution[character] for character in value)
        except KeyError as exc:
            raise ValueError("scrambled text contains a non-digit glyph") from exc

    def js_string(source: str) -> str:
        if len(source) < 2 or source[0] != "'" or source[-1] != "'" or "\\" in source:
            raise ValueError("unexpected JavaScript string")
        return source[1:-1]

    revealed: set = set()

    def reveal(cls: str) -> None:
        styles.setdefault(cls, None)
        nodes = byclass.get(cls, ())
        if len(nodes) != 1:
            raise ValueError("unexpected reveal target")
        node = nodes[0]
        declarations = ";".join(
            declaration
            for declaration in node.attrib.pop("style").split(";")
            if not HIDE.fullmatch(declaration.strip())
        )
        if declarations:
            node.set("style", declarations)
        revealed.add(node)

    def set_inner(cls: str, value: str, element: str) -> None:
        cls, value = js_string(cls), js_string(value)
        if byclass[element][0] not in revealed:
            raise ValueError("set-inner target was not revealed")
        styles.setdefault(cls, None)
        for node in byclass.get(cls, ()):
            if list(node) or "<" in value or "&" in value:
                raise ValueError("unsafe set-inner operation")
            node.text = value

    def splice(cls: str, index: str, element: str) -> None:
        cls, position = js_string(cls), int(index)
        if byclass[element][0] not in revealed:
            raise ValueError("splice target was not revealed")
        styles.setdefault(cls, None)
        for node in byclass.get(cls, ()):
            children = list(node)
            text = children[-1].tail if children and position < 0 else node.text
            if text is None or position >= len(text) or -position > len(text):
                raise ValueError("invalid splice position")
            text = text[:position] + text[position + 1 :] if position != -1 else text[:-1]
            if children and position < 0:
                children[-1].tail = text
            else:
                node.text = text

    def last_child(node):
        children = list(node)
        return last_child(children[-1]) if children else node

    def swap_last(first: str, second: str, element: str) -> None:
        first_index, second_index = int(js_string(first)), int(js_string(second))
        table = byclass[element][0]
        if table not in revealed:
            raise ValueError("swap target was not revealed")
        cells = table.xpath(".//td")
        first_node, second_node = last_child(cells[first_index]), last_child(cells[second_index])
        first_node.text, second_node.text = second_node.text, first_node.text

    ignore = re.compile(r" +|;|if *\(!lec\) *\{[^}]*\{[^}]*\}[^}]*\}|var *a *= *function\(\) *\{")
    set_inner_re = re.compile(
        r"var +([a-z_]+) *= *function\([a-z_]+, *[a-z_]+, *[a-z_]+\) *\{[^}]*\{[^}]*innerHTML *= *[a-z_]+ *;[^}]*\}[^}]*\} *;"  # noqa: E501
    )
    splice_re = re.compile(
        r"var +([a-z_]+) *= *function\([a-z_]+, *[a-z_]+, *[a-z_]+\) *\{[^}]*\{[^}]*splice[^}]*\}[^}]*\} *;"  # noqa: E501
    )
    swap_re = re.compile(
        r"var +([a-z_]+) *= *function\([a-z_]+, *[a-z_]+, *[a-z_]+\) *\{[^}]*getElementsByTagName\('td'\)[^}]*\} *;"  # noqa: E501
    )
    reveal_re = re.compile(
        r"var +([a-z_]+) *= *document\.getElementsByClassName[^}]*setTimeout\(function *\(\) *\{[^}]*\}[^)]*\) *;"  # noqa: E501
    )
    call_re = re.compile(r"([a-z_]*)\(('[^']*'), *(-?[0-9]*|'[^']*'), *([a-z_]*)\) *;")
    quit_re = re.compile(r"\} *; *document\.addEventListener\('DOMContentLoaded', *a\) *;")
    set_inner_name = splice_name = swap_name = None
    offset = 0
    while True:
        if match := ignore.match(js, offset):
            offset = match.end()
        elif match := set_inner_re.match(js, offset):
            offset, set_inner_name = match.end(), match.group(1)
        elif match := splice_re.match(js, offset):
            offset, splice_name = match.end(), match.group(1)
        elif match := swap_re.match(js, offset):
            offset, swap_name = match.end(), match.group(1)
        elif match := reveal_re.match(js, offset):
            offset = match.end()
            reveal(*match.groups())
        elif match := call_re.match(js, offset):
            offset = match.end()
            function = match.group(1)
            if function == set_inner_name:
                set_inner(*match.groups()[1:])
            elif function == splice_name:
                splice(*match.groups()[1:])
            elif function == swap_name:
                swap_last(*match.groups()[1:])
            else:
                raise ValueError("unknown CEC JavaScript transformation")
        elif match := quit_re.match(js, offset):
            offset = match.end()
            if js[offset:].strip():
                raise ValueError("trailing unrecognized CEC JavaScript")
            break
        else:
            raise ValueError("unrecognized CEC JavaScript content")

    for cls, style in afters.items():
        styles.setdefault(cls, None)
        if not style.visible or not style.content:
            continue
        if style.scramble:
            raise ValueError("scrambled pseudo-element content")
        for node in byclass.get(cls, ()):
            children = list(node)
            if children:
                children[-1].tail = (children[-1].tail or "") + style.content
            else:
                node.text = (node.text or "") + style.content

    def apply_style(node, style: Style) -> None:
        if not style.visible:
            node.drop_tree()
        elif style.scramble:
            if node.text is not None:
                node.text = unscramble(node.text)
            for descendant in node.iterdescendants():
                if descendant.text is not None:
                    descendant.text = unscramble(descendant.text)
                if descendant.tail is not None:
                    descendant.tail = unscramble(descendant.tail)

    for node in container.xpath(".//*[@style]"):
        if not all(styles.get(cls) is None for cls in node.classes):
            raise ValueError("conflicting inline and class CEC styles")
        style = parse_style(node.get("style"))
        if node in revealed:
            if style.scramble:
                raise ValueError("revealed element remains scrambled")
            continue
        del node.attrib["style"]
        apply_style(node, style)

    for cls, style in styles.items():
        for node in byclass.get(cls, ()):
            node.classes.remove(cls)
            if style is not None:
                apply_style(node, style)
    for node in container.xpath(".//span"):
        if not node.attrib:
            node.drop_tag()
    return tostring(container, encoding="unicode", method="html")
