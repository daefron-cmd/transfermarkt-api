from app.schemas.base import AuditMixin, TransfermarktBaseModel


class PlayerStat(TransfermarktBaseModel):
    competition_id: str
    competition_name: str
    season_id: str
    club_id: str
    appearances: int | None = 0
    goals: int | None = 0
    assists: int | None = 0
    yellow_cards: int | None = 0
    red_cards: int | None = 0
    minutes_played: int | None = 0


class PlayerStats(TransfermarktBaseModel, AuditMixin):
    id: str
    stats: list[PlayerStat]
