from dataclasses import dataclass
from typing import ClassVar, Self
from urllib.parse import quote

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url, trim
from app.utils.xpath import Competitions

# Crest, competition, country, clubs, players, total market value, mean market value, continent.
RESULT_CELLS = 8
RESULT_FIELDS = {
    "name": Competitions.Search.NAMES,
    "country": Competitions.Search.COUNTRIES,
    "clubs": Competitions.Search.CLUBS,
    "players": Competitions.Search.PLAYERS,
    "totalMarketValue": Competitions.Search.TOTAL_MARKET_VALUES,
    "meanMarketValue": Competitions.Search.MEAN_MARKET_VALUES,
    "continent": Competitions.Search.CONTINENTS,
}


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
        url = cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number)
        return cls(
            URL=url,
            page=parse_html(url, html),
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
        Parse and retrieve the search results for football competitions from Transfermarkt, row by row.

        Returns:
            list: A list of dictionaries, each containing details of a football competition,
                including its unique identifier, name, country (None for an international competition),
                associated clubs, number of players, total market value, mean market value, and continent.

        Raises:
            UpstreamError: If a result row does not have the expected cells or one competition link.
        """
        results = []
        for number, row in enumerate(self.page.xpath(Competitions.Search.ROWS), start=1):
            cells = row.xpath(Competitions.Search.CELLS)
            urls = row.xpath(Competitions.Search.URLS)
            if len(cells) != RESULT_CELLS or len(urls) != 1:
                raise UpstreamError(
                    502,
                    self.URL,
                    f"Competition search row {number} has {len(cells)} cells for {RESULT_CELLS} columns"
                    f" and {len(urls)} competition links",
                )
            fields = {key: trim(row.xpath(xpath)) or None for key, xpath in RESULT_FIELDS.items()}
            results.append({"id": extract_from_url(urls[0]), **fields})
        return results

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
