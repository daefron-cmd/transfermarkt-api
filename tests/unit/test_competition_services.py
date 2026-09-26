"""Tests of the HTML-backed competition services: request URLs, error details and defaults."""

import asyncio

import pytest

from app.http import UpstreamError
from app.services.competitions.clubs import TransfermarktCompetitionClubs
from app.services.competitions.search import TransfermarktCompetitionSearch
from tests.unit.tmapi_helpers import DoctoredClient, RecordingClient, fixture_bytes

TM = "https://www.transfermarkt.com"
CLUBS_URL = f"{TM}/-/startseite/wettbewerb/ES1/plus/"
SEARCH_URL = f"{TM}/schnellsuche/ergebnis/schnellsuche?query=premier&Wettbewerb_page={{page}}"


@pytest.mark.parametrize(
    ("build", "url"),
    [
        pytest.param(
            lambda html: TransfermarktCompetitionSearch.from_bytes(html, query="a/b c", page_number=3),
            f"{TM}/schnellsuche/ergebnis/schnellsuche?query=a%2Fb%20c&Wettbewerb_page=3",
            id="search",
        ),
        pytest.param(
            lambda html: TransfermarktCompetitionSearch.from_bytes(html, query="premier"),
            SEARCH_URL.format(page=1),
            id="search-default-page",
        ),
        pytest.param(
            lambda html: TransfermarktCompetitionClubs.from_bytes(html, competition_id="ES1", season_id="2023"),
            f"{CLUBS_URL}?saison_id=2023",
            id="clubs-season",
        ),
        pytest.param(
            lambda html: TransfermarktCompetitionClubs.from_bytes(html, competition_id="ES1"),
            CLUBS_URL,
            id="clubs",
        ),
    ],
)
def test_empty_body_is_502_with_the_page_url(build, url):
    with pytest.raises(UpstreamError) as e:
        build(b"  \n")
    assert (e.value.status_code, e.value.url) == (502, url)


@pytest.mark.parametrize(("kwargs", "page"), [({}, 1), ({"page_number": 3}, 3)])
def test_search_from_bytes_page_number(kwargs, page):
    tfmkt = TransfermarktCompetitionSearch.from_bytes(
        fixture_bytes(SEARCH_URL.format(page=1)), query="premier", **kwargs
    )
    assert SEARCH_URL.format(page=page) == tfmkt.URL
    assert tfmkt.search_competitions()["pageNumber"] == page


def test_search_fetch_defaults_to_the_first_page():
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktCompetitionSearch.fetch(client, query="premier"))  # type: ignore[arg-type]
    assert client.urls == [SEARCH_URL.format(page=1)]
    assert tfmkt.search_competitions()["pageNumber"] == 1


def test_search_fetch_passes_the_page_number_on():
    client = DoctoredClient({SEARCH_URL.format(page=3): fixture_bytes(SEARCH_URL.format(page=1))})
    tfmkt = asyncio.run(
        TransfermarktCompetitionSearch.fetch(client, query="premier", page_number=3)  # type: ignore[arg-type]
    )
    assert client.urls == [SEARCH_URL.format(page=3)]
    assert SEARCH_URL.format(page=3) == tfmkt.URL
    assert tfmkt.search_competitions()["pageNumber"] == 3


def clubs_page(*cells: str) -> bytes:
    """A competition page with one club cell per argument."""
    rows = "".join(f"<tr><td class='hauptlink no-border-links'>{cell}</td></tr>" for cell in cells)
    return (
        "<html><body><div class='data-header__headline-container'><h1>LaLiga</h1></div>"
        f"<a class='tm-tab' href='/-/startseite/wettbewerb/ES1/saison_id/2023'>Overview</a><table>{rows}</table>"
        "</body></html>"
    ).encode()


@pytest.mark.parametrize(
    ("cells", "reason"),
    [
        pytest.param(
            [
                "<a href='/a/startseite/verein/1'><img/></a>",
                "<a href='/b/startseite/verein/2'>B</a>",
                "<a href='/c/startseite/verein/3'>C</a>",
            ],
            "Unexpected competition clubs page: 3 ids, 2 names",
            id="link-without-text",
        ),
        pytest.param(
            ["<a href='/a/startseite/verein/1'>A</a><a href='/x'>extra</a>", "<a href='/b/startseite/verein/2'>B</a>"],
            "Unexpected competition clubs page: 2 ids, 3 names",
            id="second-link-with-text",
        ),
    ],
)
def test_competition_clubs_misaligned_links_and_names_raise(cells, reason):
    tfmkt = TransfermarktCompetitionClubs.from_bytes(clubs_page(*cells), competition_id="ES1", season_id="2023")
    with pytest.raises(UpstreamError) as e:
        tfmkt.get_competition_clubs()
    assert (e.value.status_code, e.value.url) == (502, f"{CLUBS_URL}?saison_id=2023")
    assert e.value.reason == reason


def test_competition_clubs_fetch_keeps_the_requested_season():
    url = f"{CLUBS_URL}?saison_id=2023"
    client = DoctoredClient({url: clubs_page("<a href='/a/startseite/verein/1'><img/></a>")})
    tfmkt = asyncio.run(
        TransfermarktCompetitionClubs.fetch(client, competition_id="ES1", season_id="2023")  # type: ignore[arg-type]
    )
    assert client.urls == [url]
    with pytest.raises(UpstreamError) as e:
        tfmkt.get_competition_clubs()
    assert e.value.url == url
