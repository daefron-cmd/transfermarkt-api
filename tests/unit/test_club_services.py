"""Tests of the HTML-backed club services: request URLs, error details, defaults and doctored pages."""

import asyncio

import lxml.html
import pytest
from fastapi import HTTPException

from app.http import UpstreamError
from app.schemas.clubs.players import ClubPlayers
from app.services.clubs.players import TransfermarktClubPlayers
from app.services.clubs.profile import TransfermarktClubProfile
from app.services.clubs.search import TransfermarktClubSearch
from app.utils.xpath import Clubs
from tests.unit.tmapi_helpers import DoctoredClient, RecordingClient, fixture_bytes

TM = "https://www.transfermarkt.com"
SEARCH_URL = f"{TM}/schnellsuche/ergebnis/schnellsuche?query=barcelona&Verein_page={{page}}"
SQUAD_131 = f"{TM}/-/kader/verein/131/plus/1"
SQUAD_131_2014 = f"{TM}/-/kader/verein/131/saison_id/2014/plus/1"
SQUAD_3375 = f"{TM}/-/kader/verein/3375/plus/1"

# Each service built from `html`, and the page URL it must report in its error details.
SERVICES = [
    pytest.param(
        lambda html: TransfermarktClubProfile.from_bytes(html, club_id="131"),
        "https://www.transfermarkt.us/-/datenfakten/verein/131",
        id="profile",
    ),
    pytest.param(
        lambda html: TransfermarktClubSearch.from_bytes(html, query="a/b c", page_number=3),
        f"{TM}/schnellsuche/ergebnis/schnellsuche?query=a%2Fb%20c&Verein_page=3",
        id="search",
    ),
    pytest.param(
        lambda html: TransfermarktClubSearch.from_bytes(html, query="barcelona"),
        SEARCH_URL.format(page=1),
        id="search-default-page",
    ),
    pytest.param(
        lambda html: TransfermarktClubPlayers.from_bytes(html, club_id="131", season_id="2014"),
        SQUAD_131_2014,
        id="players-season",
    ),
    pytest.param(
        lambda html: TransfermarktClubPlayers.from_bytes(html, club_id="131"),
        SQUAD_131,
        id="players",
    ),
]


@pytest.mark.parametrize(("build", "url"), SERVICES)
def test_empty_body_is_502_with_the_page_url(build, url):
    with pytest.raises(UpstreamError) as e:
        build(b"  \n")
    assert (e.value.status_code, e.value.url) == (502, url)


@pytest.mark.parametrize(("kwargs", "page"), [({}, 1), ({"page_number": 3}, 3)])
def test_search_from_bytes_page_number(kwargs, page):
    tfmkt = TransfermarktClubSearch.from_bytes(fixture_bytes(SEARCH_URL.format(page=1)), query="barcelona", **kwargs)
    assert SEARCH_URL.format(page=page) == tfmkt.URL
    assert tfmkt.search_clubs()["pageNumber"] == page


def test_search_fetch_defaults_to_the_first_page():
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktClubSearch.fetch(client, query="barcelona"))  # type: ignore[arg-type]
    assert client.urls == [SEARCH_URL.format(page=1)]
    assert tfmkt.search_clubs()["pageNumber"] == 1


def test_search_fetch_passes_the_page_number_on():
    client = DoctoredClient({SEARCH_URL.format(page=3): fixture_bytes(SEARCH_URL.format(page=1))})
    tfmkt = asyncio.run(TransfermarktClubSearch.fetch(client, query="barcelona", page_number=3))  # type: ignore[arg-type]
    assert client.urls == [SEARCH_URL.format(page=3)]
    assert SEARCH_URL.format(page=3) == tfmkt.URL
    assert tfmkt.search_clubs()["pageNumber"] == 3


def test_search_misaligned_columns_raise():
    page = lxml.html.document_fromstring(fixture_bytes(SEARCH_URL.format(page=1)))
    (flag,) = page.xpath(f"({Clubs.Search.BASE}//table[@class='items']/tbody/tr)[2]//img[@class='flaggenrahmen']")
    flag.getparent().remove(flag)
    tfmkt = TransfermarktClubSearch.from_bytes(lxml.html.tostring(page), query="barcelona")
    with pytest.raises(UpstreamError) as e:
        tfmkt.search_clubs()
    assert (e.value.status_code, e.value.url) == (502, SEARCH_URL.format(page=1))
    assert e.value.reason == (
        "Unexpected search results page: 10 ids, 10 urls, 10 names, 9 countries, 10 squads, 10 market values"
    )


def test_players_of_another_club_is_404_with_the_page_url():
    # The page has a club name, but its squad tab belongs to club 131.
    with pytest.raises(HTTPException) as e:
        TransfermarktClubPlayers.from_bytes(fixture_bytes(SQUAD_131), club_id="130")
    assert e.value.status_code == 404
    assert e.value.detail == f"Invalid request (url: {TM}/-/kader/verein/130/plus/1)"


def test_players_fetch_keeps_the_requested_season():
    # The served page is the current season's, so only the passed season_id can report 2014.
    client = DoctoredClient({SQUAD_131_2014: fixture_bytes(SQUAD_131)})
    tfmkt = asyncio.run(TransfermarktClubPlayers.fetch(client, club_id="131", season_id="2014"))  # type: ignore[arg-type]
    assert client.urls == [SQUAD_131_2014]
    assert tfmkt.URL == SQUAD_131_2014
    assert tfmkt.get_club_players()["seasonId"] == "2014"


def squad_page(url: str) -> tuple[lxml.html.HtmlElement, list]:
    page = lxml.html.document_fromstring(fixture_bytes(url))
    return page, page.xpath(Clubs.Players.ROWS)


def test_club_players_join_several_values_with_semicolons():
    page, rows = squad_page(SQUAD_131)
    cells = rows[1].xpath("./td")
    cells[6].append(lxml.html.fragment_fromstring("<span>on loan</span>"))
    cells[7].append(lxml.html.fragment_fromstring("<a><img alt='Fenerbahce B'></a>"))
    raw = TransfermarktClubPlayers.from_bytes(lxml.html.tostring(page), club_id="131").get_club_players()
    player = raw["players"][1]
    assert player["name"] == "Dominik Livakovic"
    assert player["joinedOn"] == "27/08/2026; on loan"
    assert (player["signedFrom"], player["signedFromFee"]) == ("Fenerbahce; Fenerbahce B", "€2.00m")


def club_feet(html: bytes) -> list[str | None]:
    raw = TransfermarktClubPlayers.from_bytes(html, club_id="131").get_club_players()
    return [p.foot for p in ClubPlayers.model_validate(raw).players]


def test_club_players_blank_foot_is_none_and_keeps_rows_aligned():
    before = club_feet(fixture_bytes(SQUAD_131))
    page, rows = squad_page(SQUAD_131)
    rows[2].xpath("./td")[5].text = "\xa0"
    after = club_feet(lxml.html.tostring(page))
    assert before[2] is not None
    assert after == [*before[:2], None, *before[3:]]


def test_club_players_fee_is_the_text_after_the_last_colon():
    # Without a crest alt the club is the title before its last ": ", and the fee all of the text after it.
    page, _ = squad_page(SQUAD_131_2014)
    (link,) = page.xpath("//a[@title='Liverpool FC: Ablöse €20.00m']")
    link.set("title", "Liverpool FC: U23: Ablöse €20.00 m")
    del link.xpath("./img")[0].attrib["alt"]
    raw = TransfermarktClubPlayers.from_bytes(lxml.html.tostring(page), club_id="131", season_id="2014")
    players = {p.name: p for p in ClubPlayers.model_validate(raw.get_club_players()).players}
    assert players["Javier Mascherano"].signed_from == "Liverpool FC: U23"
    assert players["Javier Mascherano"].signed_from_fee == 20_000_000


def test_national_players_without_club_and_with_several_statuses():
    page, rows = squad_page(SQUAD_3375)
    for row in rows[:2]:
        (hauptlink,) = row.xpath(".//td[@class='hauptlink']")
        hauptlink.append(lxml.html.fragment_fromstring("<span title='Injured'></span>"))
        hauptlink.append(lxml.html.fragment_fromstring("<span title='Suspended'></span>"))
    (club,) = rows[0].xpath("./td")[3].xpath("./a")
    club.getparent().remove(club)
    raw = TransfermarktClubPlayers.from_bytes(lxml.html.tostring(page), club_id="3375").get_club_players()
    first, second = raw["players"][:2]
    assert (first["name"], first["currentClub"], first["status"]) == ("David Raya", None, "Injured; Suspended")
    assert second["currentClub"] is not None
    assert second["status"] == "Injured; Suspended"
