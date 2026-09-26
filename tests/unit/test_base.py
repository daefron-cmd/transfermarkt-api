import lxml.html
import pytest

from app.services.base import TransfermarktBase

HTML = "<html><body><ul>" + "".join(f"<li>{c}</li>" for c in "abcdef") + "</ul></body></html>"


@pytest.fixture
def base() -> TransfermarktBase:
    return TransfermarktBase(URL="https://example.test", page=lxml.html.document_fromstring(HTML))


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, "abcdef"),
        ({"iloc_to": 3}, "abc"),
        ({"iloc_from": 2}, "cdef"),
        ({"iloc_from": 1, "iloc_to": 4}, "bcd"),
        ({"iloc_from": 2, "iloc_to": 5}, "cde"),
    ],
)
def test_get_text_by_xpath_slices(base, kwargs, expected):
    assert base.get_text_by_xpath("//li/text()", join_str="", **kwargs) == expected
