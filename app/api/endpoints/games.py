from fastapi import APIRouter

from app.api.deps import Client, GameId
from app.schemas import games as schemas
from app.services.games_report import TransfermarktGame

router = APIRouter()


# goals, coach, referee, stadium and several player and event fields can be null, so None values are kept.
@router.get(
    "/{game_id}",
    response_model=schemas.GameReport,
    summary="Game report",
    description="Return a game's report: result, stadium, referee, each club's coach, formation, lineup, substitutes "
    "and statistics, and the game's events (goals, cards, substitutions, penalty shootout), built from Transfermarkt's "
    "JSON API.",
)
async def get_game(game_id: GameId, client: Client) -> dict:
    tfmkt = await TransfermarktGame.fetch(client, game_id=game_id)
    game_report = tfmkt.get_game_report()
    return game_report
