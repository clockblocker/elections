import pytest

from elections.ingest.results import ACCOUNTING_FIELDS
from elections.ingest.single_member_html import parse_single_member_html

RUSSIAN_LABELS = {
    "registered_voters": "Число избирателей, включенных в список избирателей",
    "ballots_received": "Число избирательных бюллетеней, полученных участковой комиссией",
    "ballots_issued_early": "Число бюллетеней, выданных проголосовавшим досрочно",
    "ballots_issued_at_station": "Число бюллетеней, выданных в помещении для голосования",
    "ballots_issued_outside": "Число бюллетеней, выданных вне помещения для голосования",
    "ballots_cancelled": "Число погашенных избирательных бюллетеней",
    "portable_boxes_ballots": "Число бюллетеней в переносных ящиках",
    "stationary_boxes_ballots": "Число бюллетеней в стационарных ящиках",
    "invalid_ballots": "Число недействительных избирательных бюллетеней",
    "valid_ballots": "Число действительных избирательных бюллетеней",
    "lost_ballots": "Число утраченных избирательных бюллетеней",
    "unaccounted_ballots": "Число бюллетеней, не учтенных при получении",
}


def test_parses_transposed_cec_protocol_table() -> None:
    rows = ["<tr><th>Показатель</th><th>УИК № 10</th><th>УИК № 11</th></tr>"]
    for index, field in enumerate(ACCOUNTING_FIELDS, start=1):
        rows.append(
            f"<tr><td>{RUSSIAN_LABELS[field]}</td><td>{index}</td><td>{index + 100}</td></tr>"
        )
    rows.extend(
        [
            "<tr><td>1. Иванов Иван Иванович</td><td>7</td><td>8</td></tr>",
            "<tr><td>2. Петров Петр Петрович</td><td>4</td><td>5</td></tr>",
        ]
    )

    district = parse_single_member_html(
        f"<html><table>{''.join(rows)}</table></html>",
        region_name="Test Region",
        oik_code="1",
        oik_name="OIK 1",
        tik_name="Central TIK",
        source_url="https://example.test/source",
    )

    assert [item["full_name"] for item in district["candidates"]] == [
        "Иванов Иван Иванович",
        "Петров Петр Петрович",
    ]
    assert district["protocols"][0]["uik_number"] == "10"
    assert district["protocols"][0]["accounting"]["registered_voters"] == 1
    assert district["protocols"][1]["accounting"]["registered_voters"] == 101
    assert district["protocols"][1]["votes"] == {"1": 8, "2": 5}


def test_parses_deg_as_an_explicit_special_protocol() -> None:
    rows = ["<tr><th>Показатель</th><th>ДЭГ</th></tr>"]
    for index, field in enumerate(ACCOUNTING_FIELDS, start=1):
        rows.append(f"<tr><td>{RUSSIAN_LABELS[field]}</td><td>{index}</td></tr>")
    rows.append("<tr><td>1. Иванов<br>Иван Иванович</td><td>7</td></tr>")

    district = parse_single_member_html(
        f"<html><table>{''.join(rows)}</table></html>",
        region_name="Test Region",
        oik_code="1",
        oik_name="OIK 1",
        tik_name="ДЭГ",
        source_url="https://example.test/source",
    )

    assert district["candidates"][0]["full_name"] == "Иванов Иван Иванович"
    assert district["protocols"] == [
        {
            "tik_name": "ДЭГ",
            "uik_number": "ДЭГ",
            "source_record_id": "1:ДЭГ:ДЭГ",
            "source_url": "https://example.test/source",
            "accounting": {field: index for index, field in enumerate(ACCOUNTING_FIELDS, start=1)},
            "votes": {"1": 7},
        }
    ]


def test_rejects_partially_numeric_candidate_row() -> None:
    rows = ["<tr><th>Показатель</th><th>УИК № 10</th><th>УИК № 11</th></tr>"]
    for index, field in enumerate(ACCOUNTING_FIELDS, start=1):
        rows.append(f"<tr><td>{RUSSIAN_LABELS[field]}</td><td>{index}</td><td>{index}</td></tr>")
    rows.append("<tr><td>1. Иванов Иван Иванович</td><td>7</td><td>—</td></tr>")

    with pytest.raises(ValueError, match="candidate row 1 contains a non-integer"):
        parse_single_member_html(
            f"<html><table>{''.join(rows)}</table></html>",
            region_name="Test Region",
            oik_code="1",
            oik_name="OIK 1",
            tik_name="Central TIK",
            source_url="https://example.test/source",
        )


def test_parses_vertical_single_uik_cec_layout() -> None:
    rows = ["<tr><td>Наименование комиссии</td><td><b>УИК №5003</b></td></tr>"]
    for index, field in enumerate(ACCOUNTING_FIELDS, start=1):
        rows.append(
            f"<tr><td>{index}</td><td>{RUSSIAN_LABELS[field]}</td><td><b>{index * 10}</b></td></tr>"
        )
    rows.append("<tr><td>13</td><td>Иванов Иван Иванович</td><td><b>77</b></td></tr>")

    district = parse_single_member_html(
        f"<html><table>{''.join(rows)}</table></html>",
        region_name="Test Region",
        oik_code="198",
        oik_name="OIK 198",
        tik_name="Sokol",
        source_url="https://example.test/uik-5003",
    )

    assert district["candidates"][0]["position"] == 1
    assert district["protocols"][0]["uik_number"] == "5003"
    assert district["protocols"][0]["votes"] == {"1": 77}
