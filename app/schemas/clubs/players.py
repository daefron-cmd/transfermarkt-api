from datetime import date

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
    status: str | None = ""


class ClubPlayers(TransfermarktBaseModel, AuditMixin):
    id: str
    players: list[ClubPlayer]
