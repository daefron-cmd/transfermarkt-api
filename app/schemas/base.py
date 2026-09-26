from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.utils.utils import parse_date, parse_int


class AuditMixin(BaseModel):
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TransfermarktBaseModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    @field_validator(
        "date_of_birth",
        "joined_on",
        "contract",
        "founded_on",
        "members_date",
        "from_date",
        "until_date",
        "date",
        "contract_expires",
        "joined",
        "retired_since",
        "debut",
        mode="before",
        check_fields=False,
    )
    def parse_str_to_date(cls, v: str | date | None) -> date | None:
        return parse_date(v)

    @field_validator(
        "current_market_value",
        "current_transfer_record",
        "market_value",
        "mean_market_value",
        "members",
        "total_market_value",
        "age",
        "fee",
        "signed_from_fee",
        "games_missed",
        mode="before",
        check_fields=False,
    )
    def parse_str_to_int(cls, v: str | int | None) -> int | None:
        return parse_int(v)

    @field_validator("height", mode="before", check_fields=False)
    def parse_height(cls, v: str) -> int | None:
        if not v or not any(char.isdigit() for char in v):
            return None
        return int(v.replace(",", "").replace("m", "").replace("،", ""))

    @field_validator("days", mode="before", check_fields=False)
    def parse_days(cls, v: str) -> int | None:
        days = "".join(filter(str.isdigit, v))
        return int(days) if days else None
