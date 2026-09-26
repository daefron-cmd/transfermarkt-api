from fastapi import APIRouter

from app.api.deps import Client
from app.schemas import competitions as schemas
from app.services.competitions.clubs import TransfermarktCompetitionClubs
from app.services.competitions.search import TransfermarktCompetitionSearch

router = APIRouter()


@router.get("/search/{competition_name}", response_model=schemas.CompetitionSearch)
async def search_competitions(competition_name: str, client: Client, page_number: int | None = 1):
    tfmkt = await TransfermarktCompetitionSearch.fetch(client, query=competition_name, page_number=page_number)
    competitions = tfmkt.search_competitions()
    return competitions


@router.get("/{competition_id}/clubs", response_model=schemas.CompetitionClubs)
async def get_competition_clubs(competition_id: str, client: Client, season_id: str | None = None):
    tfmkt = await TransfermarktCompetitionClubs.fetch(client, competition_id=competition_id, season_id=season_id)
    competition_clubs = tfmkt.get_competition_clubs()
    return competition_clubs
