import pytest

from app.utils.regex import REGEX_COUNTRY_ID, REGEX_DOB_AGE
from app.utils.utils import safe_regex


@pytest.mark.parametrize(
    "text,dob,age",
    [
        ("24/06/1987 (39)", "24/06/1987", "39"),
        ("Jun 24, 1987 (36)", "Jun 24, 1987", "36"),
        ("05/02/1985 (41)", "05/02/1985", "41"),
        ("24/06/1987", None, None),
    ],
)
def test_regex_dob_age(text, dob, age):
    assert safe_regex(text, REGEX_DOB_AGE, "dob") == dob
    assert safe_regex(text, REGEX_DOB_AGE, "age") == age


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://tmssl.akamaized.net//images/flagge/tiny/157.png?lm=4711", "157"),
        ("https://tmssl.akamaized.net//images/flagge/tiny/40.png?lm=4711", "40"),
        ("https://img.a.transfermarkt.technology/flagge/tiny/9.png?lm=4711", "9"),
        ("https://tmssl.akamaized.net//images/flagge/tiny/157.png", "157"),
    ],
)
def test_regex_country_id(url, expected):
    assert safe_regex(url, REGEX_COUNTRY_ID, "id") == expected
