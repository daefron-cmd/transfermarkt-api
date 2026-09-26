from datetime import date

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class PlayerTransferClub(TransfermarktBaseModel):
    id: str
    name: str


class PlayerTransfer(TransfermarktBaseModel):
    id: str
    club_from: PlayerTransferClub
    club_to: PlayerTransferClub
    date: date
    upcoming: bool
    season: str
    market_value: int | None
    fee: int | None


class PlayerTransfers(TransfermarktBaseModel, AuditMixin):
    id: str
    transfers: list[PlayerTransfer]
    youth_clubs: list[str] | None
