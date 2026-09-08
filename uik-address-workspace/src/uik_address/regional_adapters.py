"""High-confidence adapters for recurring official regional directory layouts."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .models import CommissionContact, SourceEvidence


@dataclass(frozen=True, slots=True)
class AdapterResult:
    name: str
    contacts: tuple[CommissionContact, ...]


_KEMEROVO_PATH = re.compile(r"/site-tik/tik(?P<number>\d{3})/?$", re.IGNORECASE)
# The directory ordinal is not the election classifier's TIK number. This map
# was verified against the official page titles and the 2026 backbone. Missing
# ordinals 5, 36, and 44 return 404 and are intentionally not guessed.
_KEMEROVO_TIK_NUMBER = {
    1: 1,
    2: 3,
    3: 2,
    4: 4,
    6: 7,
    7: 9,
    8: 11,
    9: 12,
    10: 13,
    11: 14,
    12: 15,
    13: 16,
    14: 17,
    15: 32,
    16: 20,
    17: 21,
    18: 22,
    19: 23,
    20: 24,
    21: 25,
    22: 26,
    23: 28,
    24: 29,
    25: 30,
    26: 31,
    27: 8,
    28: 33,
    29: 34,
    30: 35,
    31: 36,
    32: 37,
    33: 38,
    34: 39,
    35: 40,
    37: 42,
    38: 43,
    39: 45,
    40: 46,
    41: 47,
    42: 5,
    43: 19,
    45: 41,
    46: 44,
    47: 27,
}


def seed_urls(subject_code: str, base_url: str) -> tuple[str, ...]:
    """Return bounded, adapter-owned directory URLs for a catalog region."""

    if subject_code != "42":
        return ()
    return tuple(
        urllib.parse.urljoin(base_url, f"/site-tik/tik{number:03d}/") for number in range(1, 48)
    )


def _label(text: str, label: str) -> str:
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(label)}\s*[:—-]?\s*([^\n]+)",
        text,
        re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else ""


def parse_html(
    *,
    subject_code: str,
    url: str,
    title: str,
    text: str,
    source: SourceEvidence,
) -> AdapterResult | None:
    """Parse an adapter-recognized page, or return ``None`` for generic handling."""

    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    match = _KEMEROVO_PATH.fullmatch(path) if subject_code == "42" else None
    if not match:
        return None

    address = _label(text, "Адрес комиссии")
    phone = _label(text, "Телефон")
    if not address and not phone:
        return AdapterResult("kemerovo_tik_directory", ())

    directory_number = int(match.group("number"))
    number = _KEMEROVO_TIK_NUMBER.get(directory_number)
    if number is None:
        return AdapterResult("kemerovo_tik_directory", ())
    name = title.strip() or f"ТИК №{number}"
    contact = CommissionContact(
        subject_code=subject_code,
        commission_type="tik",
        commission_number=number,
        commission_name=name,
        external_id="",
        commission_address=address,
        commission_phone=phone,
        voting_address="",
        voting_phone="",
        source=SourceEvidence(
            source.url,
            source.retrieved_at,
            source.sha256,
            source.status,
            "regional_adapter_kemerovo_tik",
        ),
    )
    return AdapterResult("kemerovo_tik_directory", (contact,))
