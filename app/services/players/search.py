from dataclasses import dataclass
from typing import Any, ClassVar, Self
from urllib.parse import quote

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase, parse_html
from app.utils.regex import REGEX_CHART_CLUB_ID
from app.utils.utils import extract_from_url, safe_regex, trim
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerSearch(TransfermarktBase):
    """
    A class for searching football players on Transfermarkt and retrieving search results.

    Args:
        query (str): The search query for finding football players.
        page_number (int): The page number of search results (default is 1).

    Attributes:
        URL_TEMPLATE (str): The URL template for the search query.
    """

    query: str
    page_number: int | None = 1
    URL_TEMPLATE: ClassVar[str] = (
        "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={query}&Spieler_page={page_number}"
    )

    def __post_init__(self) -> None:
        """Validate that the page contains player search results."""
        self.raise_exception_if_not_found(xpath=Players.Search.FOUND)

    @classmethod
    def from_bytes(cls, html: bytes, *, query: str, page_number: int | None = 1) -> Self:
        """Build the service from an already fetched search results page."""
        url = cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number)
        return cls(
            URL=url,
            page=parse_html(url, html),
            query=query,
            page_number=page_number,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, query: str, page_number: int | None = 1) -> Self:
        """Fetch and parse a page of player search results."""
        response = await client.get(cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number))
        return cls.from_bytes(response.content, query=query, page_number=page_number)

    def __parse_search_results(self) -> list:
        """
        Parse and return a list of player search results. Each result includes player information such as their unique
        identifier, name, position, club (including ID and name), age, nationality, and market value.

        Returns:
            list: A list of dictionaries, with each dictionary representing a player search result.
        """
        search_results: Any = self.page.xpath(Players.Search.RESULTS)
        results = []

        for result in search_results:
            idx = extract_from_url(result.xpath(Players.Search.ID))
            name = trim(result.xpath(Players.Search.NAME))
            position = trim(result.xpath(Players.Search.POSITION))
            club_name = trim(result.xpath(Players.Search.CLUB_NAME))
            club_id = safe_regex(result.xpath(Players.Search.CLUB_IMAGE), REGEX_CHART_CLUB_ID, "club_id")
            age = trim(result.xpath(Players.Search.AGE))
            nationalities = result.xpath(Players.Search.NATIONALITIES)
            market_value = trim(result.xpath(Players.Search.MARKET_VALUE))

            results.append(
                {
                    "id": idx,
                    "name": name,
                    "position": position,
                    "club": {
                        "name": club_name,
                        "id": club_id,
                    },
                    "age": age,
                    "nationalities": nationalities,
                    "marketValue": market_value,
                },
            )

        return results

    def search_players(self) -> dict:
        """
        Retrieve and parse the search results for players matching the specified query. The results
            include player information such as their name, position, club, age, nationality, and market value.

        Returns:
            dict: A dictionary containing the search query, page number, last page number and search
                results.
        """
        self.response["query"] = self.query
        self.response["pageNumber"] = self.page_number
        self.response["lastPageNumber"] = self.get_last_page_number(Players.Search.BASE)
        self.response["results"] = self.__parse_search_results()

        return self.response
