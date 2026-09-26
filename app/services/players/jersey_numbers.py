from dataclasses import dataclass
from typing import ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url, to_camel_case, zip_lists_into_dict
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerJerseyNumbers(TransfermarktBase):
    """
    A class for retrieving and parsing the players jersey numbers from Transfermarkt.

    Args:
        player_id (str): The unique identifier of the player.

    Attributes:
        URL_TEMPLATE (str): The URL template for the player's jersey numbers page on Transfermarkt.
    """

    player_id: str
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/rueckennummern/spieler/{player_id}"

    def __post_init__(self) -> None:
        """Validate that the page is a player jersey numbers page."""
        self.raise_exception_if_not_found(xpath=Players.Profile.URL)

    @classmethod
    def from_bytes(cls, html: bytes, *, player_id: str) -> Self:
        """Build the service from an already fetched jersey numbers page."""
        url = cls.URL_TEMPLATE.format(player_id=player_id)
        return cls(
            URL=url,
            page=parse_html(url, html),
            player_id=player_id,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str) -> Self:
        """Fetch and parse the player's jersey numbers page."""
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id))
        return cls.from_bytes(response.content, player_id=player_id)

    def __parse_player_jersey_numbers(self) -> list:
        """
        Parse and extract player jersey numbers data from the Transfermarkt player stats page.

        Returns:
            list: A list of dictionaries where each dictionary represents the jersey number for a specific season/club.
            Each dictionary includes keys for seasons, clubs and jersey numbers for the player.

        Raises:
            UpstreamError: If the season, club and number columns do not have the same length (the page layout changed).
        """
        headers = to_camel_case(
            ["Season", "Club", "Jersey number", *self.get_list_by_xpath(Players.JerseyNumbers.HEADERS)],
        )

        seasons = self.get_list_by_xpath(Players.JerseyNumbers.SEASONS)
        clubs_urls = self.get_list_by_xpath(Players.JerseyNumbers.CLUBS_URLS)
        clubs_ids = [extract_from_url(url) for url in clubs_urls]
        jerseynumbers = self.get_list_by_xpath(Players.JerseyNumbers.DATA)
        if not len(seasons) == len(clubs_ids) == len(jerseynumbers):
            raise UpstreamError(
                502,
                self.URL,
                f"Unexpected jersey numbers page: {len(seasons)} seasons, {len(clubs_ids)} clubs, "
                f"{len(jerseynumbers)} numbers",
            )
        data = [
            [season, club_id, number]
            for season, club_id, number in list(zip(seasons, clubs_ids, jerseynumbers, strict=True))
        ]

        return [zip_lists_into_dict(headers, stat) for stat in data]

    def get_player_jersey_numbers(self) -> dict:
        """
        Retrieve and parse player jersey numbers data for the specified player from Transfermarkt.

        Returns:
            dict: A dictionary containing the player's unique identifier, parsed player jersey numbers, and
            the timestamp of when the data was last updated.
        """
        self.response["id"] = self.player_id
        self.response["jerseyNumbers"] = self.__parse_player_jersey_numbers()

        return self.response
