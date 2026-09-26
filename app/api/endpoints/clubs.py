from fastapi import APIRouter

from app.api.deps import Client, ClubId, PageNumber, SearchQuery, SeasonId
from app.schemas import clubs as schemas
from app.services.clubs.fixtures import TransfermarktClubFixtures
from app.services.clubs.players import TransfermarktClubPlayers
from app.services.clubs.profile import TransfermarktClubProfile
from app.services.clubs.search import TransfermarktClubSearch
from app.services.clubs.squad import TransfermarktClubSquad

router = APIRouter()


@router.get(
    "/search/{club_name}",
    response_model=schemas.ClubSearch,
    response_model_exclude_none=True,
    summary="Search clubs",
    description="Search Transfermarkt clubs by name and return one page of results.",
)
async def search_clubs(club_name: SearchQuery, client: Client, page_number: PageNumber = 1) -> dict:
    tfmkt = await TransfermarktClubSearch.fetch(client, query=club_name, page_number=page_number)
    found_clubs = tfmkt.search_clubs()
    return found_clubs


@router.get(
    "/{club_id}/profile",
    response_model=schemas.ClubProfile,
    response_model_exclude_none=True,
    summary="Club profile",
    description="Return a club's profile: stadium, league, squad summary, market value and other facts.",
)
async def get_club_profile(club_id: ClubId, client: Client) -> dict:
    tfmkt = await TransfermarktClubProfile.fetch(client, club_id=club_id)
    club_profile = tfmkt.get_club_profile()
    return club_profile


@router.get(
    "/{club_id}/players",
    response_model=schemas.ClubPlayers,
    response_model_exclude_none=True,
    summary="Club squad",
    description="Return the players in a club's squad for a season (the current season by default).",
)
async def get_club_players(club_id: ClubId, client: Client, season_id: SeasonId = None) -> dict:
    tfmkt = await TransfermarktClubPlayers.fetch(client, club_id=club_id, season_id=season_id)
    club_players = tfmkt.get_club_players()
    return club_players


# seasonId, stage, goals, result and attendance can be null, so None values are kept in this response.
@router.get(
    "/{club_id}/fixtures",
    response_model=schemas.ClubFixtures,
    summary="Club fixtures",
    description="Return a club's games and results across all competitions in a season (the current season by "
    "default), built from Transfermarkt's JSON API.",
)
async def get_club_fixtures(club_id: ClubId, client: Client, season_id: SeasonId = None) -> dict:
    tfmkt = await TransfermarktClubFixtures.fetch(client, club_id=club_id, season_id=season_id)
    club_fixtures = tfmkt.get_club_fixtures()
    return club_fixtures


# seasonId and several player fields can be null, so None values are kept in this response.
@router.get(
    "/{club_id}/squad",
    response_model=schemas.ClubSquadMembers,
    summary="Club squad (JSON API)",
    description="Return a club's or national team's squad in a season (the current squad by default), with each "
    "player's shirt number, position, nationalities, contract and market value, built from Transfermarkt's JSON API.",
)
async def get_club_squad(club_id: ClubId, client: Client, season_id: SeasonId = None) -> dict:
    tfmkt = await TransfermarktClubSquad.fetch(client, club_id=club_id, season_id=season_id)
    club_squad = tfmkt.get_club_squad()
    return club_squad
