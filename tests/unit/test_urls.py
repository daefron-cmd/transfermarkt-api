"""The upstream URLs the services build from their inputs."""

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

from app.http import UpstreamResponse
from app.services.clubs.players import TransfermarktClubPlayers
from app.services.clubs.search import TransfermarktClubSearch
from app.services.competitions.clubs import TransfermarktCompetitionClubs
from app.services.competitions.search import TransfermarktCompetitionSearch
from app.services.players.search import TransfermarktPlayerSearch


class FakeClient:
    """Records the requested URLs and answers with an empty page (which the services reject with a 404)."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str) -> UpstreamResponse:
        self.urls.append(url)
        return UpstreamResponse(url=url, status_code=200, content=b"<html><body></body></html>")


def fetched_url(service, **kwargs) -> str:
    client = FakeClient()
    with contextlib.suppress(HTTPException):
        asyncio.run(service.fetch(client, **kwargs))
    assert len(client.urls) == 1
    return client.urls[0]


@pytest.mark.parametrize(
    "service,page_param",
    [
        (TransfermarktPlayerSearch, "Spieler_page"),
        (TransfermarktClubSearch, "Verein_page"),
        (TransfermarktCompetitionSearch, "Wettbewerb_page"),
    ],
)
def test_search_query_is_url_encoded(service, page_param):
    url = fetched_url(service, query="a&b c/d?e#f", page_number=2)

    assert url == (
        f"https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query=a%26b%20c%2Fd%3Fe%23f&{page_param}=2"
    )


def test_club_players_url_without_season_has_no_season_segment():
    url = fetched_url(TransfermarktClubPlayers, club_id="131")

    assert url == "https://www.transfermarkt.com/-/kader/verein/131/plus/1"


def test_club_players_url_with_season():
    url = fetched_url(TransfermarktClubPlayers, club_id="131", season_id="2014")

    assert url == "https://www.transfermarkt.com/-/kader/verein/131/saison_id/2014/plus/1"


def test_competition_clubs_url_without_season_has_no_season_param():
    url = fetched_url(TransfermarktCompetitionClubs, competition_id="ES1")

    assert url == "https://www.transfermarkt.com/-/startseite/wettbewerb/ES1/plus/"


def test_competition_clubs_url_with_season():
    url = fetched_url(TransfermarktCompetitionClubs, competition_id="ES1", season_id="2023")

    assert url == "https://www.transfermarkt.com/-/startseite/wettbewerb/ES1/plus/?saison_id=2023"
