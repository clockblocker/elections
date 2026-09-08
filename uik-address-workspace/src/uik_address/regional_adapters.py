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
_VOLOGDA_PATH = re.compile(
    r"/izbiratelnye-komissii/territorialnye-izbiratelnye-komissii/T(?P<number>\d{2})\.php$",
    re.IGNORECASE,
)
_YAMAL_ADDRESS_PATH = re.compile(
    r"/about/tik/(?P<directory_number>\d{2})tik/adress/index\.php$",
    re.IGNORECASE,
)
_BELGOROD_PATH = re.compile(r"/tik/(?P<slug>[a-z-]+)/?$", re.IGNORECASE)
_UGRA_PATH = re.compile(
    r"/izbiratelnie-komissii/tik/tikpage/tik(?P<directory_number>\d{2})/priem-og/index\.php$",
    re.IGNORECASE,
)
_PENZA_PATH = re.compile(r"/tik_page/tik_(?P<directory_number>\d{2})/index\.php$", re.IGNORECASE)
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

# Verified from the official directory pages and the 2026 CEC backbone.  The
# missing directory number 02 is the Labytangi commission, which has no 2026
# backbone TIK record, so it is deliberately not emitted.
_YAMAL_TIK_NUMBER = {1: 2, 3: 4, 4: 5, 6: 7, 9: 10, 10: 11}

# The Belgorod commission directory has human-readable municipality slugs.  The
# mapping below was verified from each page breadcrumb against the 2026 CEC
# backbone; it is not an assumption about alphabetical ordering.
_BELGOROD_TIK_NUMBER = {
    "alekseevka": 1,
    "bel-city": 2,
    "belgorod": 3,
    "borisovka": 4,
    "valuyki": 5,
    "veydelevka": 6,
    "volokonovka": 7,
    "grayvoron": 8,
    "gubkin": 9,
    "ivnya": 10,
    "korocha": 11,
    "krasnoe": 12,
    "biruch": 13,
    "kryaruga": 14,
    "noskol": 15,
    "prohorovka": 16,
    "rakitnoe": 17,
    "rovenki": 18,
    "stoskol": 19,
    "chernyanka": 20,
    "shebekino": 21,
    "stroitel": 22,
}

# Yugra's directory ordinal differs from the 2026 TIK number.  Each entry was
# verified from the canonical page title and the 2026 CEC backbone.  Directory
# 03 (Kogalym) was unavailable in the archived crawl, so is intentionally not
# emitted until its current identity is independently verified.
_UGRA_TIK_NUMBER = {
    1: 1,
    2: 2,
    4: 4,
    5: 5,
    6: 6,
    7: 8,
    8: 10,
    9: 11,
    10: 12,
    11: 13,
    12: 14,
    13: 15,
    14: 16,
    15: 18,
    16: 17,
    17: 19,
    18: 20,
    19: 7,
    20: 21,
    21: 22,
}

# Verified from each official TIK page title against the 2026 backbone.  The
# site's route ordinal is an editorial directory position, not a TIK number.
_PENZA_TIK_NUMBER = {
    1: 7,
    2: 32,
    3: 8,
    4: 9,
    5: 10,
    6: 11,
    7: 12,
    8: 14,
    9: 15,
    10: 16,
    11: 17,
    12: 18,
    14: 6,
    15: 20,
    16: 21,
    17: 22,
    18: 23,
    19: 24,
    20: 25,
    21: 26,
    22: 27,
    23: 28,
    24: 5,
    25: 2,
    26: 3,
    27: 1,
    28: 4,
    29: 29,
    30: 30,
    31: 31,
    32: 33,
    33: 34,
    35: 19,
}


def seed_urls(subject_code: str, base_url: str) -> tuple[str, ...]:
    """Return bounded, adapter-owned directory URLs for a catalog region."""

    if subject_code == "42":
        return tuple(
            urllib.parse.urljoin(base_url, f"/site-tik/tik{number:03d}/") for number in range(1, 48)
        )
    if subject_code == "35":
        return tuple(
            urllib.parse.urljoin(
                base_url,
                f"/izbiratelnye-komissii/territorialnye-izbiratelnye-komissii/T{number:02d}.php",
            )
            for number in range(1, 25)
        )
    if subject_code == "89":
        return tuple(
            urllib.parse.urljoin(base_url, f"/about/tik/{number:02d}tik/adress/index.php")
            for number in sorted(_YAMAL_TIK_NUMBER)
        )
    if subject_code == "31":
        return tuple(
            urllib.parse.urljoin(base_url, f"/tik/{slug}/") for slug in _BELGOROD_TIK_NUMBER
        )
    if subject_code == "86":
        return tuple(
            urllib.parse.urljoin(
                base_url,
                f"/izbiratelnie-komissii/tik/tikpage/tik{directory_number:02d}/priem-og/index.php",
            )
            for directory_number in _UGRA_TIK_NUMBER
        )
    if subject_code == "58":
        return tuple(
            urllib.parse.urljoin(base_url, f"/tik_page/tik_{number:02d}/index.php")
            for number in _PENZA_TIK_NUMBER
        )
    return ()


def _label(text: str, label: str) -> str:
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(label)}\s*[:—-]?\s*([^\n]+)",
        text,
        re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else ""


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return " ".join(match.group("value").split()) if match else ""


def _tik_contact(
    *,
    subject_code: str,
    number: int,
    name: str,
    address: str,
    phone: str,
    source: SourceEvidence,
    source_type: str,
) -> CommissionContact:
    return CommissionContact(
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
            source.url, source.retrieved_at, source.sha256, source.status, source_type
        ),
    )


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
    if subject_code == "31":
        match = _BELGOROD_PATH.fullmatch(path)
        if not match:
            return None
        number = _BELGOROD_TIK_NUMBER.get(match.group("slug").casefold())
        if number is None:
            return AdapterResult("belgorod_tik_directory", ())
        address = _first(
            r"(?:фактический\s+адрес|адрес)\s*:\s*(?P<value>.+?)(?=\s*(?:телефоны?|e-?mail)\s*:)",
            text,
        )
        phone = _first(r"\bтелефоны?\s*:\s*(?P<value>\+?7?[0-9()\-\s,]{5,})", text)
        if not address and not phone:
            return AdapterResult("belgorod_tik_directory", ())
        return AdapterResult(
            "belgorod_tik_directory",
            (
                _tik_contact(
                    subject_code=subject_code,
                    number=number,
                    name=f"ТИК №{number}",
                    address=address.rstrip(" ,.;"),
                    phone=phone.rstrip(" ,.;"),
                    source=source,
                    source_type="regional_adapter_belgorod_tik",
                ),
            ),
        )

    if subject_code == "86":
        match = _UGRA_PATH.fullmatch(path)
        if not match:
            return None
        number = _UGRA_TIK_NUMBER.get(int(match.group("directory_number")))
        if number is None:
            return AdapterResult("ugra_tik_directory", ())
        address = _first(
            r"\bАдрес\s+комиссии\s*:\s*(?P<value>.+?)(?=\s*(?:телефон|факс)\s*:)",
            text,
        )
        phone = _first(r"\bТелефон\s*:\s*(?P<value>[+0-9()\-\s,]{5,})", text)
        if not address and not phone:
            return AdapterResult("ugra_tik_directory", ())
        return AdapterResult(
            "ugra_tik_directory",
            (
                _tik_contact(
                    subject_code=subject_code,
                    number=number,
                    name=title.strip() or f"ТИК №{number}",
                    address=address.rstrip(" ,.;"),
                    phone=phone.rstrip(" ,.;"),
                    source=source,
                    source_type="regional_adapter_ugra_tik",
                ),
            ),
        )

    if subject_code == "58":
        match = _PENZA_PATH.fullmatch(path)
        if not match:
            return None
        number = _PENZA_TIK_NUMBER.get(int(match.group("directory_number")))
        if number is None:
            return AdapterResult("penza_tik_directory", ())
        # The first labelled pair is in the TIK page body; a later shared
        # regional-commission footer is deliberately outside this bounded pair.
        address = _first(r"\bадрес\s*:\s*(?P<value>.+?)(?=\s*(?:телефоны?|e-?mail)\s*:)", text)
        phone = _first(r"\bтелефоны?\s*:\s*(?P<value>[+0-9()\-\s,]{5,})", text)
        if not address and not phone:
            return AdapterResult("penza_tik_directory", ())
        return AdapterResult(
            "penza_tik_directory",
            (
                _tik_contact(
                    subject_code=subject_code,
                    number=number,
                    name=title.strip() or f"ТИК №{number}",
                    address=address.rstrip(" ,.;"),
                    phone=phone.rstrip(" ,.;"),
                    source=source,
                    source_type="regional_adapter_penza_tik",
                ),
            ),
        )

    if subject_code == "35":
        match = _VOLOGDA_PATH.fullmatch(path)
        if not match:
            return None
        number = int(match.group("number"))
        # The official Vologda directory uses the current TIK ordinal for
        # T01--T24; this is verified against the backbone, not inferred.
        if not 1 <= number <= 24:
            return AdapterResult("vologda_tik_directory", ())
        address = _first(
            r"находится\s+по\s+адресу\s*:\s*(?P<value>.+?)(?=\s*(?:телефон|номера?\s+телефона|тел\.)\s*[:.,])",
            text,
        )
        phone = _first(
            r"(?:телефон(?:/факс)?|номера?\s+телефона(?:,\s*факса)?|тел\.)\s*[:.,]?\s*(?P<value>(?:\+?7|8|\()[0-9()\-\s]{5,})",
            text,
        )
        if not address and not phone:
            return AdapterResult("vologda_tik_directory", ())
        return AdapterResult(
            "vologda_tik_directory",
            (
                _tik_contact(
                    subject_code=subject_code,
                    number=number,
                    name=title.strip() or f"ТИК №{number}",
                    address=address.rstrip(","),
                    phone=phone.rstrip(" ,.;"),
                    source=source,
                    source_type="regional_adapter_vologda_tik",
                ),
            ),
        )

    if subject_code == "89":
        match = _YAMAL_ADDRESS_PATH.fullmatch(path)
        if not match:
            return None
        number = _YAMAL_TIK_NUMBER.get(int(match.group("directory_number")))
        if number is None:
            return AdapterResult("yamal_tik_address_directory", ())
        address = _first(r"\bАдрес\s*:\s*(?P<value>.+?)(?=\s*тел\.?\s*[:(])", text)
        phone = _first(r"\bтел\.?\s*:?\s*(?P<value>\(?[0-9][0-9()\-\s,]{5,})", text)
        if not address and not phone:
            return AdapterResult("yamal_tik_address_directory", ())
        return AdapterResult(
            "yamal_tik_address_directory",
            (
                _tik_contact(
                    subject_code=subject_code,
                    number=number,
                    name=f"ТИК №{number}",
                    address=address.rstrip(" ,.;"),
                    phone=phone.rstrip(" ,.;"),
                    source=source,
                    source_type="regional_adapter_yamal_tik",
                ),
            ),
        )

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
    contact = _tik_contact(
        subject_code=subject_code,
        number=number,
        name=name,
        address=address,
        phone=phone,
        source=source,
        source_type="regional_adapter_kemerovo_tik",
    )
    return AdapterResult("kemerovo_tik_directory", (contact,))
