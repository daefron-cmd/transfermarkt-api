from datetime import date
from enum import Enum

from pydantic import HttpUrl

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class PlayerPlaceOfBirth(TransfermarktBaseModel):
    city: str | None
    country: str | None


class PlayerPosition(TransfermarktBaseModel):
    main: str | None
    other: list[str] | None


class PlayerClub(TransfermarktBaseModel):
    id: str | None
    name: str
    joined: date | None
    contract_expires: date | None
    contract_option: str | None
    # Retired player
    last_club_id: str | None
    last_club_name: str | None
    most_games_for: str | None


class PlayerAgent(TransfermarktBaseModel):
    name: str | None
    url: str | None


class TrainerProfile(TransfermarktBaseModel):
    id: str | None
    url: str | None
    position: str | None


class RelativeProfileTypeEnum(str, Enum):  # noqa: UP042 - StrEnum changes str() output
    PLAYER = "player"
    TRAINER = "trainer"


class Relatives(TransfermarktBaseModel):
    id: str
    url: str
    name: str
    profile_type: RelativeProfileTypeEnum


class PlayerProfile(TransfermarktBaseModel, AuditMixin):
    id: str
    url: HttpUrl
    name: str
    description: str
    full_name: str | None
    name_in_home_country: str | None
    image_url: HttpUrl | None
    date_of_birth: date | None
    place_of_birth: PlayerPlaceOfBirth
    age: int | None
    height: int | None
    citizenship: list[str]
    is_retired: bool
    retired_since: date | None
    position: PlayerPosition
    foot: str | None
    shirt_number: str | None
    club: PlayerClub
    market_value: int | None
    agent: PlayerAgent | None
    outfitter: str | None
    socialMedia: list[str] | None
    trainer_profile: TrainerProfile | None
    relatives: list[Relatives] | None
