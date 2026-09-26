from dataclasses import dataclass
from typing import Any, ClassVar, Self

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url, trim
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerInjuries(TransfermarktBase):
    """
    Represents a service for retrieving and parsing the injury history of a football player on Transfermarkt.

    Args:
        player_id (str): The unique identifier of the player.
        page_number (int): The page number of the player's injury history.

    Attributes:
        URL_TEMPLATE (str): The URL template to fetch the player's injury history data.
    """

    player_id: str
    page_number: int | None = 1
    URL_TEMPLATE: ClassVar[str] = (
        "https://www.transfermarkt.com/player/verletzungen/spieler/{player_id}/plus/1/page/{page_number}"
    )

    def __post_init__(self) -> None:
        """Validate that the page is a player injuries page."""
        self.raise_exception_if_not_found(xpath=Players.Profile.URL)

    @classmethod
    def from_bytes(cls, html: bytes, *, player_id: str, page_number: int | None = 1) -> Self:
        """Build the service from an already fetched injuries page."""
        url = cls.URL_TEMPLATE.format(player_id=player_id, page_number=page_number)
        return cls(
            URL=url,
            page=parse_html(url, html),
            player_id=player_id,
            page_number=page_number,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str, page_number: int | None = 1) -> Self:
        """Fetch and parse a page of the player's injury history."""
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id, page_number=page_number))
        return cls.from_bytes(response.content, player_id=player_id, page_number=page_number)

    def __parse_player_injuries(self) -> list[dict] | None:
        """
        Parse the injury history of a football player from the retrieved data.

        Returns:
            list: A list of dictionaries, where each dictionary represents an injury in the
                player's injury history. Each dictionary contains keys 'season', 'injury', 'fromDate',
                'untilDate', 'days', 'gamesMissed', and 'gamesMissedClubs' with their respective values.

        """
        injuries: Any = self.page.xpath(Players.Injuries.RESULTS)
        player_injuries = []

        for injury in injuries:
            season = trim(injury.xpath(Players.Injuries.SEASONS))
            injury_type = trim(injury.xpath(Players.Injuries.INJURY))
            date_from = trim(injury.xpath(Players.Injuries.FROM))
            date_until = trim(injury.xpath(Players.Injuries.UNTIL))
            days = trim(injury.xpath(Players.Injuries.DAYS))
            games_missed = trim(injury.xpath(Players.Injuries.GAMES_MISSED))
            games_missed_clubs_urls = injury.xpath(Players.Injuries.GAMES_MISSED_CLUBS_URLS)
            games_missed_clubs_ids = [extract_from_url(club_url) for club_url in games_missed_clubs_urls]

            player_injuries.append(
                {
                    "season": season,
                    "injury": injury_type,
                    "fromDate": date_from,
                    "untilDate": date_until,
                    "days": days,
                    "gamesMissed": games_missed,
                    "gamesMissedClubs": games_missed_clubs_ids,
                },
            )

        return player_injuries

    def get_player_injuries(self) -> dict:
        """
        Retrieve and parse the injury history of a football player.

        Returns:
            dict: A dictionary containing the player's unique identifier, current page number,
                last page number, and injury history.

        """
        self.response["id"] = self.player_id
        self.response["pageNumber"] = self.page_number
        self.response["lastPageNumber"] = self.get_last_page_number()
        self.response["injuries"] = self.__parse_player_injuries()

        return self.response
