from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.base import AuditMixin


class CompetitionTableRow(BaseModel):
    # Built from tmapi JSON, so the fields are already typed; this skips TransfermarktBaseModel's text parsers.
    model_config = ConfigDict(alias_generator=to_camel)

    club_id: str
    club_name: str
    position: int
    previous_position: int | None
    played: int
    won: int
    drawn: int
    lost: int
    goals_for: int
    goals_against: int
    goal_difference: int
    points: int
    points_deducted: int | None
    zone: str | None


class CompetitionTableGroup(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    name: str
    rows: list[CompetitionTableRow]


class CompetitionTable(AuditMixin):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str
    season_id: str
    tables: list[CompetitionTableGroup]
