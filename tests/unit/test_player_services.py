"""Tests of the HTML-backed player services: request URLs, error details, defaults and doctored pages."""

import asyncio
import json

import lxml.html
import pytest
from fastapi import HTTPException

from app.http import UpstreamError, UpstreamResponse
from app.schemas.players.achievements import PlayerAchievements
from app.services.players.achievements import TransfermarktPlayerAchievements
from app.services.players.injuries import TransfermarktPlayerInjuries
from app.services.players.jersey_numbers import TransfermarktPlayerJerseyNumbers
from app.services.players.market_value import TransfermarktPlayerMarketValue
from app.services.players.profile import TransfermarktPlayerProfile
from app.services.players.search import TransfermarktPlayerSearch
from app.services.players.transfers import TransfermarktPlayerTransfers
from tests.unit.tmapi_helpers import RecordingClient, fixture_bytes

TM = "https://www.transfermarkt.com"
INJURIES_28003 = f"{TM}/player/verletzungen/spieler/28003/plus/1/page/1"

# Each service built from `html`, and the page URL it must report in its error details.
SERVICES = [
    pytest.param(
        lambda html: TransfermarktPlayerProfile.from_bytes(html, player_id="28003"),
        f"{TM}/-/profil/spieler/28003",
        id="profile",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerSearch.from_bytes(html, query="a/b c", page_number=3),
        f"{TM}/schnellsuche/ergebnis/schnellsuche?query=a%2Fb%20c&Spieler_page=3",
        id="search",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerSearch.from_bytes(html, query="messi"),
        f"{TM}/schnellsuche/ergebnis/schnellsuche?query=messi&Spieler_page=1",
        id="search-default-page",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerMarketValue.from_bytes(html, player_id="28003"),
        f"{TM}/-/marktwertverlauf/spieler/28003",
        id="market_value",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerTransfers.from_bytes(html, player_id="28003"),
        f"{TM}/-/transfers/spieler/28003",
        id="transfers",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerJerseyNumbers.from_bytes(html, player_id="28003"),
        f"{TM}/-/rueckennummern/spieler/28003",
        id="jersey_numbers",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerInjuries.from_bytes(html, player_id="28003", page_number=2),
        f"{TM}/player/verletzungen/spieler/28003/plus/1/page/2",
        id="injuries",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerInjuries.from_bytes(html, player_id="28003"),
        INJURIES_28003,
        id="injuries-default-page",
    ),
    pytest.param(
        lambda html: TransfermarktPlayerAchievements.from_bytes(html, player_id="28003"),
        f"{TM}/-/erfolge/spieler/28003",
        id="achievements",
    ),
]


@pytest.mark.parametrize(("build", "url"), SERVICES)
def test_page_without_content_is_404_with_the_page_url(build, url):
    with pytest.raises(HTTPException) as e:
        build(b"<html><body></body></html>")
    assert e.value.status_code == 404
    assert e.value.detail == f"Invalid request (url: {url})"


@pytest.mark.parametrize(("build", "url"), SERVICES)
def test_empty_body_is_502_with_the_page_url(build, url):
    with pytest.raises(UpstreamError) as e:
        build(b"  \n")
    assert (e.value.status_code, e.value.url) == (502, url)


class StubClient:
    """A client that serves `content` for every URL and records the requested URLs."""

    def __init__(self, content: bytes):
        self.content = content
        self.urls: list[str] = []

    async def get(self, url: str) -> UpstreamResponse:
        self.urls.append(url)
        return UpstreamResponse(url=url, status_code=200, content=self.content)


def test_search_fetch_defaults_to_the_first_page():
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktPlayerSearch.fetch(client, query="messi"))  # type: ignore[arg-type]
    assert client.urls == [f"{TM}/schnellsuche/ergebnis/schnellsuche?query=messi&Spieler_page=1"]
    assert tfmkt.search_players()["pageNumber"] == 1


def test_injuries_fetch_defaults_to_the_first_page():
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktPlayerInjuries.fetch(client, player_id="28003"))  # type: ignore[arg-type]
    assert client.urls == [INJURIES_28003]
    assert tfmkt.get_player_injuries()["pageNumber"] == 1


def test_injuries_fetch_passes_the_page_number_on():
    client = StubClient(fixture_bytes(INJURIES_28003))
    tfmkt = asyncio.run(TransfermarktPlayerInjuries.fetch(client, player_id="28003", page_number=2))  # type: ignore[arg-type]
    assert client.urls == [f"{TM}/player/verletzungen/spieler/28003/plus/1/page/2"]
    assert tfmkt.get_player_injuries()["pageNumber"] == 2


@pytest.mark.parametrize(("page_number", "expected"), [(None, 1), (2, 2)])
def test_injuries_page_number(page_number, expected):
    kwargs = {} if page_number is None else {"page_number": page_number}
    tfmkt = TransfermarktPlayerInjuries.from_bytes(fixture_bytes(INJURIES_28003), player_id="28003", **kwargs)
    assert tfmkt.get_player_injuries()["pageNumber"] == expected


def test_market_value_from_bytes_reads_the_chart():
    html = fixture_bytes(f"{TM}/-/marktwertverlauf/spieler/28003")
    chart = fixture_bytes(TransfermarktPlayerMarketValue.URL_MARKET_VALUE.format(player_id="28003"))
    assert TransfermarktPlayerMarketValue.from_bytes(html, chart, player_id="28003").market_value_chart == json.loads(
        chart
    )
    assert TransfermarktPlayerMarketValue.from_bytes(html, player_id="28003").market_value_chart == {}


def test_transfers_from_bytes_reads_the_history():
    html = fixture_bytes(f"{TM}/-/transfers/spieler/28003")
    history = fixture_bytes(TransfermarktPlayerTransfers.URL_TRANSFERS.format(player_id="28003"))
    assert TransfermarktPlayerTransfers.from_bytes(html, history, player_id="28003").transfer_history == json.loads(
        history
    )
    assert TransfermarktPlayerTransfers.from_bytes(html, player_id="28003").transfer_history == {}


def test_profile_of_a_retired_player():
    page = lxml.html.document_fromstring(fixture_bytes(f"{TM}/-/profil/spieler/28003"))
    assert page.body is not None
    page.body.insert(
        0,
        lxml.html.fragment_fromstring(
            "<div><span>Retired since: <span>01/07/2025</span></span>"
            "<span>Last club: <span><a title='FC Barcelona' href='/fc-barcelona/startseite/verein/131'>Barça</a>"
            "</span></span></div>"
        ),
    )
    tfmkt = TransfermarktPlayerProfile.from_bytes(lxml.html.tostring(page), player_id="28003")
    assert tfmkt.player_id == "28003"
    profile = tfmkt.get_player_profile()
    assert (profile["isRetired"], profile["retiredSince"]) == (True, "01/07/2025")
    assert (profile["club"]["lastClubId"], profile["club"]["lastClubName"]) == ("131", "FC Barcelona")


ACHIEVEMENTS_PAGE = """
<html><head><link rel="canonical" href="https://www.transfermarkt.com/x/erfolge/spieler/1"></head><body>
<div class="box"><h2>Olympian</h2><table class="auflistung">
  <tr>
    <td class="erfolg_table_saison">2008</td>
    <td><a href="/verein/131" title="FC Barcelona">FC Barcelona</a></td>
    <td><a href="/wettbewerb/OLYM">Olympic Games</a></td>
  </tr>
  <tr><td class="erfolg_table_saison">2012</td></tr>
</table></div>
</body></html>
"""


def test_achievements_keep_names_without_ids_and_titles_without_a_count():
    # A link whose URL carries no id still names its club or competition; a title with no "Nx " prefix is kept whole.
    achievements = TransfermarktPlayerAchievements.from_bytes(ACHIEVEMENTS_PAGE.encode(), player_id="1")
    assert achievements.get_player_achievements()["achievements"] == [
        {
            "title": "Olympian",
            "count": 2,
            "details": [
                {
                    "season": {"id": None, "name": "2008"},
                    "club": {"id": None, "name": "FC Barcelona"},
                    "competition": {"id": None, "name": "Olympic Games"},
                },
                {"season": {"id": None, "name": "2012"}},
            ],
        },
    ]


UNNAMED_LINKS_PAGE = """
<html><head><link rel="canonical" href="https://www.transfermarkt.com/x/erfolge/spieler/1"></head><body>
<div class="box"><h2>2x Champion</h2><table class="auflistung">
  <tr>
    <td class="erfolg_table_saison">20/21</td>
    <td><a href="/laliga/startseite/wettbewerb/ES1/saison_id/2020"></a></td>
  </tr>
  <tr>
    <td class="erfolg_table_saison">21/22</td>
    <td><a href="/fc-barcelona/startseite/verein/131/saison_id/2021" title="">FCB</a></td>
  </tr>
</table></div>
</body></html>
"""


def test_achievements_keep_ids_without_names_and_omit_the_name():
    # A competition link with no text and a club link with an empty title still identify the entry; the name is null.
    raw = TransfermarktPlayerAchievements.from_bytes(
        UNNAMED_LINKS_PAGE.encode(), player_id="1"
    ).get_player_achievements()
    assert raw["achievements"][0]["details"] == [
        {"season": {"id": "2020", "name": "20/21"}, "competition": {"id": "ES1", "name": None}},
        {"season": {"id": "2021", "name": "21/22"}, "club": {"id": "131", "name": None}},
    ]
    # Serialised as the endpoint does (response_model_exclude_none), the missing name is left out.
    response = PlayerAchievements.model_validate(raw).model_dump(mode="json", by_alias=True, exclude_none=True)
    assert response["achievements"][0]["details"] == [
        {"season": {"id": "2020", "name": "20/21"}, "competition": {"id": "ES1"}},
        {"season": {"id": "2021", "name": "21/22"}, "club": {"id": "131"}},
    ]


JERSEY_NUMBERS_URL = f"{TM}/-/rueckennummern/spieler/28003"


@pytest.mark.parametrize(
    ("cell", "counts"),
    [
        ("zentriert", "54 seasons, 55 clubs, 55 numbers"),
        ("hauptlink no-border-links", "55 seasons, 54 clubs, 55 numbers"),
        ("zentriert hauptlink", "55 seasons, 55 clubs, 54 numbers"),
    ],
)
def test_jersey_numbers_misaligned_columns_raise(cell, counts):
    page = lxml.html.document_fromstring(fixture_bytes(JERSEY_NUMBERS_URL))
    (td,) = page.xpath(f"(//table[@class='items']//tbody/tr)[3]/td[@class='{cell}']")
    td.getparent().remove(td)
    tfmkt = TransfermarktPlayerJerseyNumbers.from_bytes(lxml.html.tostring(page), player_id="28003")
    with pytest.raises(UpstreamError) as e:
        tfmkt.get_player_jersey_numbers()
    assert (e.value.status_code, e.value.url) == (502, JERSEY_NUMBERS_URL)
    assert e.value.reason == f"Unexpected jersey numbers page: {counts}"


def achievement_box(title: str, seasons: list[str]) -> str:
    rows = "".join(f"<tr><td class='erfolg_table_saison'>{season}</td></tr>" for season in seasons)
    return f"<div class='box'><h2>{title}</h2><table class='auflistung'>{rows}</table></div>"


def test_achievements_title_tolerates_any_whitespace_after_the_count():
    seasons = ["21/22", "22/23", "23/24", "24/25"]
    page = (
        "<html><head><link rel='canonical' href='https://www.transfermarkt.com/x/erfolge/spieler/1'></head><body>"
        f"{achievement_box('4x  Title', seasons)}{achievement_box('4x\tTitle', seasons)}</body></html>"
    )
    achievements = TransfermarktPlayerAchievements.from_bytes(page.encode(), player_id="1").get_player_achievements()
    assert [(a["title"], a["count"]) for a in achievements["achievements"]] == [("Title", 4), ("Title", 4)]
