from dataclasses import dataclass
from typing import ClassVar, Self
from urllib.parse import quote

import lxml.html

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase
from app.utils.utils import extract_from_url
from app.utils.xpath import Competitions


@dataclass
class TransfermarktCompetitionSearch(TransfermarktBase):
    """
    A class for searching football competitions on Transfermarkt and retrieving search results.

    Args:
        query (str): The search query for finding football competitions.
        page_number (int): The page number of search results (default is 1).

    Attributes:
        URL_TEMPLATE (str): The URL template for the search query.
    """

    query: str
    page_number: int | None = 1
    URL_TEMPLATE: ClassVar[str] = (
        "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={query}&Wettbewerb_page={page_number}"
    )

    @classmethod
    def from_bytes(cls, html: bytes, *, query: str, page_number: int | None = 1) -> Self:
        """Build the service from an already fetched search results page."""
        return cls(
            URL=cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number),
            page=lxml.html.document_fromstring(html),
            query=query,
            page_number=page_number,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, query: str, page_number: int | None = 1) -> Self:
        """Fetch and parse a page of competition search results."""
        response = await client.get(cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number))
        return cls.from_bytes(response.content, query=query, page_number=page_number)

    def __parse_search_results(self) -> list:
        """
        Parse and retrieve the search results for football competitions from Transfermarkt.

        Returns:
            list: A list of dictionaries, each containing details of a football competition,
                including its unique identifier, name, country, associated clubs, number of players,
                total market value, mean market value, and continent.
        """
        idx = [extract_from_url(url) for url in self.get_list_by_xpath(Competitions.Search.URLS)]
        name = self.get_list_by_xpath(Competitions.Search.NAMES)
        country = self.get_list_by_xpath(Competitions.Search.COUNTRIES)
        clubs = self.get_list_by_xpath(Competitions.Search.CLUBS)
        players = self.get_list_by_xpath(Competitions.Search.PLAYERS)
        total_market_value = self.get_list_by_xpath(Competitions.Search.TOTAL_MARKET_VALUES)
        mean_market_value = self.get_list_by_xpath(Competitions.Search.MEAN_MARKET_VALUES)
        continent = self.get_list_by_xpath(Competitions.Search.CONTINENTS)

        return [
            {
                "id": idx,
                "name": name,
                "country": country,
                "clubs": clubs,
                "players": players,
                "totalMarketValue": total_market_value,
                "meanMarketValue": mean_market_value,
                "continent": continent,
            }
            for idx, name, country, clubs, players, total_market_value, mean_market_value, continent in zip(
                idx,
                name,
                country,
                clubs,
                players,
                total_market_value,
                mean_market_value,
                continent,
                strict=False,
            )
        ]

    def search_competitions(self) -> dict:
        """
        Perform a search for football competitions and retrieve the search results.

        Returns:
            dict: A dictionary containing search results, including competition details.
        """
        self.response["query"] = self.query
        self.response["pageNumber"] = self.page_number
        self.response["lastPageNumber"] = self.get_last_page_number(Competitions.Search.BASE)
        self.response["results"] = self.__parse_search_results()

        return self.response
