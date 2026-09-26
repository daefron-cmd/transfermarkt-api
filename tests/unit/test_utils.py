from datetime import date

import pytest

from app.utils.utils import (
    extract_from_url,
    parse_date,
    parse_int,
    remove_str,
    safe_split,
    to_camel_case,
    trim,
)


def test_trim_strips_whitespace_and_nbsp():
    assert trim("  a\xa0b  ") == "ab"


def test_trim_joins_list():
    assert trim([" a", "b "]) == "ab"


@pytest.mark.parametrize(
    "url,element,expected",
    [
        ("/fc-barcelona/startseite/verein/131", "id", "131"),
        ("/fc-barcelona/startseite/verein/131", "season_id", None),
        ("/fc-barcelona/startseite/verein/131/saison_id/2023", "id", "131"),
        ("/fc-barcelona/startseite/verein/131/saison_id/2023", "season_id", "2023"),
        ("/lionel-messi/profil/spieler/28003/transfer_id/12345", "id", "28003"),
        ("/lionel-messi/profil/spieler/28003/transfer_id/12345", "transfer_id", "12345"),
        (None, "id", None),
        ("", "id", None),
    ],
)
def test_extract_from_url(url, element, expected):
    assert extract_from_url(url, element) == expected


@pytest.mark.parametrize(
    "url,element,expected",
    [
        ("https://www.transfermarkt.com/fc-barcelona/startseite/verein/131", "id", "131"),
        ("https://www.transfermarkt.us/fc-barcelona/startseite/verein/131/saison_id/2023", "season_id", "2023"),
        ("https://www.transfermarkt.com/lionel-messi/profil/spieler/28003?foo=bar", "id", "28003"),
        ("https://www.transfermarkt.com/", "id", None),
        ("https://www.transfermarkt.com/only/two", "id", None),
        ("not a url", "id", None),
    ],
)
def test_extract_from_url_absolute_url(url, element, expected):
    assert extract_from_url(url, element) == expected


def test_to_camel_case():
    assert to_camel_case(["Season", "Jersey number", "competition name"]) == [
        "season",
        "jerseyNumber",
        "competitionName",
    ]


def test_safe_split():
    assert safe_split(" a / b ", "/") == ["a", "b"]


def test_safe_split_non_string():
    assert safe_split(None, "/") is None


def test_remove_str_list():
    assert remove_str(" hello world ", ["hello"]) == "world"


def test_remove_str_string_is_one_substring():
    assert remove_str("Pos 2", "Pos") == "2"
    assert remove_str("abcab", "ab") == "c"
    assert remove_str("bacba", "ab") == "bacba"


def test_remove_str_non_string():
    assert remove_str(None, "x") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        # dd/mm/yyyy, including the ambiguous case that a month-first parser gets wrong
        ("04/05/2001", date(2001, 5, 4)),
        ("15/07/2023", date(2023, 7, 15)),
        ("01/07/2025", date(2025, 7, 1)),
        (" 24/06/1987 ", date(1987, 6, 24)),
        # dd.mm.yyyy
        ("04.05.2001", date(2001, 5, 4)),
        # Mon d, yyyy (older English format, still served by transfermarkt.us club pages)
        ("Jan 1, 2022", date(2022, 1, 1)),
        ("Nov 29, 1899", date(1899, 11, 29)),
        # yyyy-mm-dd
        ("2001-05-04", date(2001, 5, 4)),
        # dateutil fallback, day first
        ("4 May 2001", date(2001, 5, 4)),
        ("04-05-2001", date(2001, 5, 4)),
        # empty and placeholder values
        ("-", None),
        ("", None),
        (None, None),
        ("N/A", None),
        ("unknown", None),
        ("31/02/2001", None),
        # already a date
        (date(2001, 5, 4), date(2001, 5, 4)),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        # magnitude suffix: "." and "," are decimal separators
        ("€1.20m", 1_200_000),
        ("€950k", 950_000),
        ("€1.5bn", 1_500_000_000),
        ("€1.26bn", 1_260_000_000),
        ("-€94.50m", -94_500_000),
        ("€-94.50m", -94_500_000),
        ("€1,5m", 1_500_000),
        ("€1.15m", 1_150_000),
        ("€2b", 2_000_000_000),
        # no suffix: "." and "," are thousands separators
        ("170.000", 170_000),
        ("1.234'", 1_234),
        ("62.657", 62_657),
        ("62,657", 62_657),
        ("7", 7),
        ("+3", 3),
        (" 27 ", 27),
        # HTML around the value
        ("<span>€1.20m</span>", 1_200_000),
        ('<i class="normaler-text">Loan fee:</i><br/>€1.20m', 1_200_000),
        ("<span>free transfer</span>", None),
        # empty and placeholder values
        ("-", None),
        ("", None),
        (None, None),
        ("free transfer", None),
        ("?", None),
        # already an int
        (5, 5),
    ],
)
def test_parse_int(value, expected):
    assert parse_int(value) == expected
