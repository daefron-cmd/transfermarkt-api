import json
from dataclasses import dataclass, field
from typing import ClassVar, Self

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase, parse_html
from app.utils.regex import REGEX_CHART_CLUB_ID
from app.utils.utils import safe_regex, zip_lists_into_dict
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerMarketValue(TransfermarktBase):
    """
    Represents a service for retrieving and parsing the market value history of a football player on Transfermarkt.

    Args:
        player_id (str): The unique identifier of the player.
        market_value_chart (dict): The player's market value history chart data (JSON).

    Attributes:
        URL_TEMPLATE (str): The URL template to fetch the player's market value data.
        URL_MARKET_VALUE (str): The URL template to fetch the player's market value history chart data.
    """

    player_id: str
    market_value_chart: dict = field(default_factory=dict)
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/marktwertverlauf/spieler/{player_id}"
    URL_MARKET_VALUE: ClassVar[str] = "https://www.transfermarkt.com/ceapi/marketValueDevelopment/graph/{player_id}"

    def __post_init__(self) -> None:
        """Validate that the page is a player market value page."""
        self.raise_exception_if_not_found(xpath=Players.Profile.NAME)

    @classmethod
    def from_bytes(cls, html: bytes, market_value_chart: bytes | None = None, *, player_id: str) -> Self:
        """Build the service from an already fetched market value page and, optionally, its chart JSON."""
        url = cls.URL_TEMPLATE.format(player_id=player_id)
        return cls(
            URL=url,
            page=parse_html(url, html),
            player_id=player_id,
            market_value_chart=json.loads(market_value_chart) if market_value_chart is not None else {},
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str) -> Self:
        """Fetch and parse the market value page, then (for a valid player) its chart data."""
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id))
        tfmkt = cls.from_bytes(response.content, player_id=player_id)
        chart = await client.get(cls.URL_MARKET_VALUE.format(player_id=player_id))
        tfmkt.market_value_chart = chart.json()
        return tfmkt

    def __parse_market_value_history(self) -> list:
        """
        Parse the market value history of a football player from the retrieved data.

        Returns:
            list: A list of dictionaries, where each dictionary represents a data point in the
                player's market value history. Each dictionary contains keys 'date', 'age',
                'clubId', 'clubName', and 'marketValue' with their respective values.
        """
        data = self.market_value_chart["list"]

        club_image = None
        for entry in data:
            entry["date"] = entry.pop("datum_mw")
            entry["clubName"] = entry.pop("verein")
            entry["marketValue"] = entry.pop("mw")
            if not entry.get("wappen"):
                entry["wappen"] = club_image
            else:
                club_image = entry["wappen"]
            entry["clubId"] = safe_regex(entry["wappen"], REGEX_CHART_CLUB_ID, "club_id")

        return [
            {key: entry[key] for key in entry if key in ["date", "age", "clubId", "clubName", "marketValue"]}
            for entry in data
        ]

    def get_player_market_value(self) -> dict:
        """
        Retrieve and parse the market value history of a football player.

        Returns:
            dict: A dictionary containing the player's unique identifier, current market value,
                market value history, and ranking.
        """
        self.response["id"] = self.player_id
        self.response["marketValue"] = self.get_text_by_xpath(Players.MarketValue.CURRENT, join_str="")
        self.response["marketValueHistory"] = self.__parse_market_value_history()
        self.response["ranking"] = zip_lists_into_dict(
            self.get_list_by_xpath(Players.MarketValue.RANKINGS_NAMES),
            self.get_list_by_xpath(Players.MarketValue.RANKINGS_POSITIONS),
        )

        return self.response
