from datetime import date

from pydantic import RootModel, model_validator

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class MarketValueHistory(TransfermarktBaseModel):
    age: int
    date: date
    club_id: str
    club_name: str
    market_value: int | None = None


class PlayerRanking(RootModel):
    root: dict[str, int]

    @model_validator(mode="before")
    def parse_ranking_values(cls, v: dict[str, str]) -> dict[str, int]:
        """Parse the ranking values from string to int.

        E.g.: {"Worldwide": "1.234"} -> {"Worldwide": 1234}
        """
        return {k: int(v.replace(".", "")) for k, v in v.items()}


class PlayerMarketValue(TransfermarktBaseModel, AuditMixin):
    id: str
    market_value: int | None
    marketValueHistory: list[MarketValueHistory]
    ranking: PlayerRanking
