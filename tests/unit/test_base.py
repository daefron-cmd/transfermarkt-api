from pathlib import Path

import lxml.html
import pytest

from app.http import UpstreamError
from app.services.base import TransfermarktBase, parse_html

HTML = "<html><body><ul>" + "".join(f"<li>{c}</li>" for c in "abcdef") + "</ul></body></html>"
URL = "https://www.transfermarkt.com/-/profil/spieler/28003"
SERVICES_DIR = Path(__file__).parents[2] / "app" / "services"


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


def test_parse_html_returns_the_document():
    page = parse_html(URL, HTML.encode())

    assert page.xpath("//li/text()") == list("abcdef")


@pytest.mark.parametrize("content", [b"", b" \r\n\t "])
def test_parse_html_empty_body_is_a_502(content):
    with pytest.raises(UpstreamError) as exc_info:
        parse_html(URL, content)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == f"Empty upstream response ({len(content)} bytes) for url: {URL}"


def test_parse_html_unparseable_body_is_a_502():
    # lxml finds no element in a comment-only document and raises ParserError("Document is empty").
    with pytest.raises(UpstreamError) as exc_info:
        parse_html(URL, b"<!-- nothing here -->")

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == f"Unparseable upstream response for url: {URL}"


def test_services_parse_html_only_through_parse_html():
    callers = sorted(
        str(path.relative_to(SERVICES_DIR))
        for path in SERVICES_DIR.rglob("*.py")
        if "document_fromstring(" in path.read_text()
    )

    assert callers == ["base.py"]
