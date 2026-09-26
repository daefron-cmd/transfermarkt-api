from app.schemas.base import AuditMixin, TransfermarktBaseModel


class ClubSearchResult(TransfermarktBaseModel):
    id: str
    url: str
    name: str
    country: str
    squad: int
    market_value: int | None = None


class ClubSearch(TransfermarktBaseModel, AuditMixin):
    query: str
    page_number: int
    last_page_number: int
    results: list[ClubSearchResult]
