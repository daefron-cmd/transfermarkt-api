from app.schemas.base import AuditMixin, TransfermarktBaseModel


class AchievementDetail(TransfermarktBaseModel):
    id: str | None = None
    name: str | None = None


class AchievementDetails(TransfermarktBaseModel):
    competition: AchievementDetail | None = None
    season: AchievementDetail
    club: AchievementDetail | None = None


class PlayerAchievement(TransfermarktBaseModel):
    title: str
    count: int
    details: list[AchievementDetails]


class PlayerAchievements(TransfermarktBaseModel, AuditMixin):
    id: str
    achievements: list[PlayerAchievement]
