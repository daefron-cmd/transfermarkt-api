from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import Client, PageNumber, PlayerId, SearchQuery
from app.schemas import players as schemas
from app.services.players.achievements import TransfermarktPlayerAchievements
from app.services.players.injuries import TransfermarktPlayerInjuries
from app.services.players.jersey_numbers import TransfermarktPlayerJerseyNumbers
from app.services.players.market_value import TransfermarktPlayerMarketValue
from app.services.players.profile import TransfermarktPlayerProfile
from app.services.players.search import TransfermarktPlayerSearch
from app.services.players.stats import TransfermarktPlayerStats
from app.services.players.transfers import TransfermarktPlayerTransfers

router = APIRouter()


@router.get(
    "/search/{player_name}",
    response_model=schemas.PlayerSearch,
    response_model_exclude_none=True,
    summary="Search players",
    description="Search Transfermarkt players by name and return one page of results.",
)
async def search_players(player_name: SearchQuery, client: Client, page_number: PageNumber = 1):
    tfmkt = await TransfermarktPlayerSearch.fetch(client, query=player_name, page_number=page_number)
    found_players = tfmkt.search_players()
    return found_players


@router.get(
    "/{player_id}/profile",
    response_model=schemas.PlayerProfile,
    response_model_exclude_none=True,
    summary="Player profile",
    description="Return a player's profile: personal details, position, club, contract and market value.",
)
async def get_player_profile(player_id: PlayerId, client: Client):
    tfmkt = await TransfermarktPlayerProfile.fetch(client, player_id=player_id)
    player_info = tfmkt.get_player_profile()
    return player_info


@router.get(
    "/{player_id}/market_value",
    response_model=schemas.PlayerMarketValue,
    response_model_exclude_none=True,
    summary="Player market value history",
    description="Return a player's current market value, its history and the player's market value rankings.",
)
async def get_player_market_value(player_id: PlayerId, client: Client):
    tfmkt = await TransfermarktPlayerMarketValue.fetch(client, player_id=player_id)
    player_market_value = tfmkt.get_player_market_value()
    return player_market_value


@router.get(
    "/{player_id}/transfers",
    response_model=schemas.PlayerTransfers,
    response_model_exclude_none=True,
    summary="Player transfers",
    description="Return a player's transfer history with clubs, dates and fees.",
)
async def get_player_transfers(player_id: PlayerId, client: Client):
    tfmkt = await TransfermarktPlayerTransfers.fetch(client, player_id=player_id)
    player_market_value = tfmkt.get_player_transfers()
    return player_market_value


@router.get(
    "/{player_id}/jersey_numbers",
    response_model=schemas.PlayerJerseyNumbers,
    response_model_exclude_none=True,
    summary="Player jersey numbers",
    description="Return the jersey numbers a player has worn per season and club.",
)
async def get_player_jersey_numbers(player_id: PlayerId, client: Client):
    tfmkt = await TransfermarktPlayerJerseyNumbers.fetch(client, player_id=player_id)
    player_jerseynumbers = tfmkt.get_player_jersey_numbers()
    return player_jerseynumbers


# goalsConceded and cleanSheets are null for outfield players, so None values are kept in this response.
@router.get(
    "/{player_id}/stats",
    response_model=schemas.PlayerStats,
    summary="Player stats",
    description="Return a player's statistics per season, competition and club, built from Transfermarkt's JSON API.",
)
async def get_player_stats(
    player_id: PlayerId,
    client: Client,
    season_id: Annotated[
        str | None,
        Query(
            pattern=r"^[0-9]{4}$",
            description="Only return this season, by its starting year (2024 = 24/25). All seasons when omitted.",
            examples=["2024"],
        ),
    ] = None,
):
    tfmkt = await TransfermarktPlayerStats.fetch(client, player_id=player_id, season_id=season_id)
    player_stats = tfmkt.get_player_stats()
    return player_stats


@router.get(
    "/{player_id}/injuries",
    response_model=schemas.PlayerInjuries,
    response_model_exclude_none=True,
    summary="Player injuries",
    description="Return one page of a player's injury history.",
)
async def get_player_injuries(player_id: PlayerId, client: Client, page_number: PageNumber = 1):
    tfmkt = await TransfermarktPlayerInjuries.fetch(client, player_id=player_id, page_number=page_number)
    players_injuries = tfmkt.get_player_injuries()
    return players_injuries


@router.get(
    "/{player_id}/achievements",
    response_model=schemas.PlayerAchievements,
    response_model_exclude_none=True,
    summary="Player achievements",
    description="Return the titles and awards a player has won.",
)
async def get_player_achievements(player_id: PlayerId, client: Client):
    tfmkt = await TransfermarktPlayerAchievements.fetch(client, player_id=player_id)
    player_achievements = tfmkt.get_player_achievements()
    return player_achievements
