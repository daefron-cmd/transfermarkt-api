from app.schemas.base import AuditMixin, TransfermarktBaseModel


class CompetitionSearchResult(TransfermarktBaseModel):
    id: str
    name: str
    country: str | None = None
    clubs: int
    players: int
    total_market_value: int | None = None
    mean_market_value: int | None = None
    continent: str | None = None


class CompetitionSearch(TransfermarktBaseModel, AuditMixin):
    query: str
    page_number: int
    last_page_number: int
    results: list[CompetitionSearchResult]
