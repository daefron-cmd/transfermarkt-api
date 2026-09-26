from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class GameClub(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str


class ShootoutScore(BaseModel):
    home: int
    away: int


class Game(BaseModel):
    # Built from tmapi JSON, so the fields are already typed; see app.services.games for the mapping.
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    season_id: str
    season_name: str
    competition_id: str
    competition_name: str
    matchday: int
    stage: str | None
    date: datetime
    is_time_defined: bool
    home_club: GameClub
    away_club: GameClub
    home_goals: int | None
    away_goals: int | None
    ended_after: str | None
    shootout: ShootoutScore | None
    is_finished: bool
    is_live: bool
    attendance: int | None
    url: str
