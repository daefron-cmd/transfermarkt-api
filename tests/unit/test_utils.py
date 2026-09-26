import pytest

from app.utils.utils import extract_from_url, remove_str, safe_split, to_camel_case, trim


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


def test_extract_from_url_absolute_url_raises():
    # Current behaviour: the regex is anchored to a relative path, so re.match returns None
    # and .groupdict() raises AttributeError (only TypeError is caught).
    with pytest.raises(AttributeError):
        extract_from_url("https://www.transfermarkt.com/fc-barcelona/startseite/verein/131")


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


def test_remove_str_string_removes_each_character():
    # Current behaviour: a str argument is turned into a list of its characters.
    assert remove_str(" €1.5m ", "€m") == "1.5"
    assert remove_str("abcab", "ab") == "c"


def test_remove_str_non_string():
    assert remove_str(None, "x") is None
