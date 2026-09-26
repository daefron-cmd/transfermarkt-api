from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.base import AuditMixin


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


class GamePerson(BaseModel):
    id: str
    name: str


class GameStadium(BaseModel):
    id: str
    name: str
    city: str | None


class GameReportPlayer(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str
    shirt_number: int | None
    is_captain: bool
    position: str | None
    market_value: int | None
    age: int | None


class GameReportSide(BaseModel):
    club: GameClub
    coach: GamePerson | None
    formation: str | None
    lineup: list[GameReportPlayer]
    substitutes: list[GameReportPlayer]
    # tmapi's raw per-club statistics (clubStatistics), passed through unchanged.
    statistics: dict[str, Any] | None


class GameEventScore(BaseModel):
    home: int
    away: int


class GameEvent(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    type: str
    minute: int
    added_time: int
    club: GameClub | None
    action: str | None
    reason: str | None
    player: GamePerson | None
    related_player: GamePerson | None
    score: GameEventScore | None


class GameReport(Game, AuditMixin):
    # Built from a tmapi game report; see app.services.games_report for the mapping.
    stage_label: str | None
    duration: int | None
    stadium: GameStadium | None
    referee: GamePerson | None
    home: GameReportSide
    away: GameReportSide
    events: list[GameEvent]
