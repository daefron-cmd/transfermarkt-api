from datetime import date

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class Injury(TransfermarktBaseModel):
    season: str
    injury: str
    from_date: date
    until_date: date | None
    days: int
    games_missed: int | None
    games_missed_clubs: list[str]


class PlayerInjuries(TransfermarktBaseModel, AuditMixin):
    id: str
    page_number: int
    last_page_number: int
    injuries: list[Injury]
