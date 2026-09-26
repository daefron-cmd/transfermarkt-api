from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.base import AuditMixin
from app.schemas.games import Game


class ClubGame(Game):
    venue: str
    result: str | None


class ClubFixtures(AuditMixin):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str
    season_id: str | None
    games: list[ClubGame]
