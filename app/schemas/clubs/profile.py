from datetime import date

from app.schemas.base import TransfermarktBaseModel


class ClubSquad(TransfermarktBaseModel):
    size: int
    average_age: float
    foreigners: int
    national_team_players: int


class ClubLeague(TransfermarktBaseModel):
    id: str | None = None
    name: str | None = None
    country_id: str | None = None
    country_name: str | None = None
    tier: str | None = None


class ClubProfile(TransfermarktBaseModel):
    id: str
    url: str
    name: str
    official_name: str | None = None
    image: str
    legal_form: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    address_line_3: str | None = None
    tel: str | None = None
    fax: str | None = None
    website: str | None = None
    founded_on: date | None = None
    members: int | None = None
    members_date: date | None = None
    other_sports: list[str] | None = None
    colors: list[str] | None = []
    stadium_name: str
    stadium_seats: int
    current_transfer_record: int
    current_market_value: int | None = None
    confederation: str | None = None
    fifa_world_ranking: str | None = None
    squad: ClubSquad
    league: ClubLeague
    historical_crests: list[str] | None = []
