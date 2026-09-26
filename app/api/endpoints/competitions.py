from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import Client, CompetitionId, PageNumber, SearchQuery, SeasonId
from app.schemas import competitions as schemas
from app.services.competitions.clubs import TransfermarktCompetitionClubs
from app.services.competitions.fixtures import TransfermarktCompetitionFixtures
from app.services.competitions.search import TransfermarktCompetitionSearch
from app.services.competitions.table import TransfermarktCompetitionTable

router = APIRouter()


@router.get(
    "/search/{competition_name}",
    response_model=schemas.CompetitionSearch,
    response_model_exclude_none=True,
    summary="Search competitions",
    description="Search Transfermarkt competitions by name and return one page of results.",
)
async def search_competitions(competition_name: SearchQuery, client: Client, page_number: PageNumber = 1):
    tfmkt = await TransfermarktCompetitionSearch.fetch(client, query=competition_name, page_number=page_number)
    competitions = tfmkt.search_competitions()
    return competitions


@router.get(
    "/{competition_id}/clubs",
    response_model=schemas.CompetitionClubs,
    response_model_exclude_none=True,
    summary="Competition clubs",
    description="Return the clubs taking part in a competition in a season (the current season by default).",
)
async def get_competition_clubs(competition_id: CompetitionId, client: Client, season_id: SeasonId = None):
    tfmkt = await TransfermarktCompetitionClubs.fetch(client, competition_id=competition_id, season_id=season_id)
    competition_clubs = tfmkt.get_competition_clubs()
    return competition_clubs


# previousPosition, pointsDeducted and zone can be null, so None values are kept in this response.
@router.get(
    "/{competition_id}/table",
    response_model=schemas.CompetitionTable,
    summary="Competition table",
    description="Return a competition's league table, or one table per group, in a season (the current season by "
    "default), built from Transfermarkt's JSON API. Knockout cups have no tables.",
)
async def get_competition_table(competition_id: CompetitionId, client: Client, season_id: SeasonId = None):
    tfmkt = await TransfermarktCompetitionTable.fetch(client, competition_id=competition_id, season_id=season_id)
    competition_table = tfmkt.get_competition_table()
    return competition_table


# stage, goals and attendance can be null, so None values are kept in this response.
@router.get(
    "/{competition_id}/fixtures",
    response_model=schemas.CompetitionFixtures,
    summary="Competition fixtures",
    description="Return a competition's games and results in a season (the current season by default), optionally "
    "for one matchday, built from Transfermarkt's JSON API.",
)
async def get_competition_fixtures(
    competition_id: CompetitionId,
    client: Client,
    season_id: SeasonId = None,
    matchday: Annotated[
        int | None,
        Query(ge=1, description="Only return this matchday's (round's) games. All games when omitted.", examples=[1]),
    ] = None,
):
    tfmkt = await TransfermarktCompetitionFixtures.fetch(
        client, competition_id=competition_id, season_id=season_id, matchday=matchday
    )
    competition_fixtures = tfmkt.get_competition_fixtures()
    return competition_fixtures
