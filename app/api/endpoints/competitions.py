from fastapi import APIRouter

from app.api.deps import Client, CompetitionId, PageNumber, SearchQuery, SeasonId
from app.schemas import competitions as schemas
from app.services.competitions.clubs import TransfermarktCompetitionClubs
from app.services.competitions.search import TransfermarktCompetitionSearch

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
