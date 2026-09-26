from datetime import date

from pydantic import field_validator

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class ClubPlayer(TransfermarktBaseModel):
    id: str
    name: str
    position: str
    date_of_birth: date | None = None
    age: int | None = None
    nationality: list[str]
    current_club: str | None = None
    height: int | None = None
    foot: str | None = None
    joined_on: date | None = None
    joined: str | None = None
    signed_from: str | None = None
    contract: date | None = None
    market_value: int | None = None
    status: str | None = None

    # The service joins xpath results into these strings, which gives "" when there is nothing to join.
    @field_validator("joined_on", "joined", "signed_from", "status", mode="before")
    def blank_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class ClubPlayers(TransfermarktBaseModel, AuditMixin):
    id: str
    players: list[ClubPlayer]
