from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.base import AuditMixin


class PlayerStat(BaseModel):
    # Built from tmapi JSON, so the fields are already typed; this skips TransfermarktBaseModel's text parsers.
    model_config = ConfigDict(alias_generator=to_camel)

    season_id: str
    season_name: str
    competition_id: str
    competition_name: str
    club_id: str
    club_name: str
    appearances: int
    goals: int
    assists: int
    own_goals: int
    penalty_goals: int
    yellow_cards: int
    second_yellow_cards: int
    red_cards: int
    minutes_played: int
    goals_conceded: int | None
    clean_sheets: int | None


class PlayerStats(AuditMixin):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    stats: list[PlayerStat]
