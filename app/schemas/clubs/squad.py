from datetime import date

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.base import AuditMixin


class ClubSquadMember(BaseModel):
    # Built from tmapi JSON, so the fields are already typed; this skips TransfermarktBaseModel's text parsers.
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str
    shirt_number: int | None
    is_captain: bool
    position: str | None
    date_of_birth: date | None
    age: int | None
    nationalities: list[str]
    height: int | None
    foot: str | None
    contract_until: date | None
    market_value: int | None
    type: str


class ClubSquadMembers(AuditMixin):
    model_config = ConfigDict(alias_generator=to_camel)

    id: str
    name: str
    is_national_team: bool
    season_id: str | None
    players: list[ClubSquadMember]
