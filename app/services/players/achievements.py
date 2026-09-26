from dataclasses import dataclass
from typing import ClassVar, Self

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url, trim
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerAchievements(TransfermarktBase):
    """
    Represents a service for retrieving and parsing the achievements of a football player on Transfermarkt.

    Args:
        player_id (str): The unique identifier of the player.

    Attributes:
        URL_TEMPLATE (str): The URL template to fetch the player's achievements data.
    """

    player_id: str
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/erfolge/spieler/{player_id}"

    def __post_init__(self) -> None:
        """Validate that the page is a player achievements page."""
        self.raise_exception_if_not_found(xpath=Players.Profile.URL)

    @classmethod
    def from_bytes(cls, html: bytes, *, player_id: str) -> Self:
        """Build the service from an already fetched achievements page."""
        url = cls.URL_TEMPLATE.format(player_id=player_id)
        return cls(
            URL=url,
            page=parse_html(url, html),
            player_id=player_id,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str) -> Self:
        """Fetch and parse the player's achievements page."""
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id))
        return cls.from_bytes(response.content, player_id=player_id)

    def __parse_player_achievements(self) -> list:
        """
        Parse the achievements of a football player from the retrieved data.

        Returns:
            list: A list of dictionaries, where each dictionary represents an achievement in the
                player's achievement history. Each dictionary contains keys 'title', 'count', and
                'details' with their respective values.
        """

        achievements = self.page.xpath(Players.Achievements.ACHIEVEMENTS)

        player_achievements = []
        for achievement in achievements:
            title = trim(achievement.xpath(Players.Achievements.TITLE)).split(None, 1)[-1]
            details = achievement.xpath(Players.Achievements.DETAILS)

            achievement_details = []
            for detail in details:
                competition_name = trim(detail.xpath(Players.Achievements.COMPETITION_NAME))
                competition_url = trim(detail.xpath(Players.Achievements.COMPETITION_URL))
                competition_id = extract_from_url(competition_url)
                season_name = trim(detail.xpath(Players.Achievements.SEASON))
                club_name = trim(detail.xpath(Players.Achievements.CLUB_NAME))
                club_url = trim(detail.xpath(Players.Achievements.CLUB_URL))
                club_id = extract_from_url(club_url)

                achievement_detail = {
                    "season": {
                        "id": extract_from_url(club_url, "season_id") or extract_from_url(competition_url, "season_id"),
                        "name": season_name,
                    },
                }

                if club_id or club_name:
                    achievement_detail["club"] = {
                        "id": club_id,
                        "name": club_name,
                    }

                if competition_id or competition_name:
                    achievement_detail["competition"] = {
                        "id": competition_id,
                        "name": competition_name or None,
                    }

                achievement_details.append(achievement_detail)

            player_achievements.append(
                {
                    "title": title,
                    "count": len(details),
                    "details": achievement_details,
                },
            )

        return player_achievements

    def get_player_achievements(self) -> dict:
        """
        Retrieve and parse the achievements of a football player.

        Returns:
            dict: A dictionary containing the player's unique identifier and achievement history.
        """

        self.response["id"] = self.player_id
        self.response["achievements"] = self.__parse_player_achievements()

        return self.response
