from pathlib import Path

import pytest
from schema import Regex

UNIT_DIR = Path(__file__).parent / "unit"


def pytest_collection_modifyitems(items):
    for item in items:
        if not item.path.is_relative_to(UNIT_DIR):
            item.add_marker(pytest.mark.live)


@pytest.fixture
def len_greater_than_0():
    return lambda x: len(x) > 0


@pytest.fixture
def len_equal_to_0():
    return lambda x: len(x) == 0


@pytest.fixture
def regex_club_url():
    return Regex(r"^/\w.+/startseite/verein/\d+$")


@pytest.fixture
def regex_date_mmm_dd_yyyy():
    return Regex(r"^(\w+\s\d+,\s\d+)|(-)$")


@pytest.fixture
def regex_market_value():
    return Regex(r"^(€\d+\.\d+.(m|bn))|(€\d+.k)|(-)$")


@pytest.fixture
def regex_value_variation():
    return Regex(r"^(\+|-)?€(\+|-)?(\d.+)(k|m)$")


@pytest.fixture
def regex_integer():
    return Regex(r"^(\d+|-)$")


@pytest.fixture
def regex_height():
    return Regex(r"^(\d+,\d+m)|(m)$")
