from app.schemas.base import AuditMixin, TransfermarktBaseModel


class PlayerSearchClub(TransfermarktBaseModel):
    id: str
    name: str


class PlayerSearchResult(TransfermarktBaseModel):
    id: str
    name: str
    position: str
    club: PlayerSearchClub
    age: int | None
    nationalities: list[str]
    market_value: int | None


class PlayerSearch(TransfermarktBaseModel, AuditMixin):
    query: str
    pageNumber: int
    lastPageNumber: int
    results: list[PlayerSearchResult]
