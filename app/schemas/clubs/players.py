from datetime import date

from pydantic import field_validator

from app.schemas.base import AuditMixin, TransfermarktBaseModel


class ClubPlayer(TransfermarktBaseModel):
    """
    A player of a club's or a national team's squad.

    A national team's squad has currentClub (the player's club), internationalMatches, internationalGoals (both 0 where
    the page shows "-"), debut and status, and no nationality, joinedOn, signedFrom, signedFromFee or contract.
    """

    id: str
    name: str
    position: str
    date_of_birth: date | None = None
    age: int | None = None
    nationality: list[str] | None = None
    current_club: str | None = None
    height: int | None = None
    foot: str | None = None
    joined_on: date | None = None
    signed_from: str | None = None
    signed_from_fee: int | None = None
    contract: date | None = None
    market_value: int | None = None
    status: str | None = None
    international_matches: int | None = None
    international_goals: int | None = None
    debut: date | None = None

    # The service joins xpath results into these strings, which gives "" when there is nothing to join; a blank foot
    # cell is "" too.
    @field_validator("joined_on", "signed_from", "status", "foot", mode="before")
    def blank_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v


class ClubPlayers(TransfermarktBaseModel, AuditMixin):
    id: str
    season_id: str
    players: list[ClubPlayer]
