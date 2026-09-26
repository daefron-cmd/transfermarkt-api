from dataclasses import dataclass
from typing import ClassVar, Self
from urllib.parse import quote

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url
from app.utils.xpath import Clubs


@dataclass
class TransfermarktClubSearch(TransfermarktBase):
    """
    A class for searching football clubs on Transfermarkt and retrieving search results.

    Args:
        query (str): The search query for finding football clubs.
        page_number (int): The page number of search results (default is 1).

    Attributes:
        URL_TEMPLATE (str): The URL template for the search query.
    """

    query: str
    page_number: int | None = 1
    URL_TEMPLATE: ClassVar[str] = (
        "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={query}&Verein_page={page_number}"
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
        """Fetch and parse a page of club search results."""
        response = await client.get(cls.URL_TEMPLATE.format(query=quote(query, safe=""), page_number=page_number))
        return cls.from_bytes(response.content, query=query, page_number=page_number)

    def __parse_search_results(self) -> list:
        """
        Parse the search results page and extract information about the found football clubs.

        Returns:
            list: A list of dictionaries, where each dictionary contains information about a
                football club found in the search results, including the club's unique identifier,
                URL, name, country, squad size, and market value.

        Raises:
            UpstreamError: If the result columns do not have one value per club (the page layout changed).
        """
        clubs_names = self.get_list_by_xpath(Clubs.Search.NAMES)
        clubs_urls = self.get_list_by_xpath(Clubs.Search.URLS)
        clubs_countries = self.get_list_by_xpath(Clubs.Search.COUNTRIES)
        clubs_squads = self.get_list_by_xpath(Clubs.Search.SQUADS)
        clubs_market_values = self.get_list_by_xpath(Clubs.Search.MARKET_VALUES)
        clubs_ids = [extract_from_url(url) for url in clubs_urls]
        columns = (clubs_ids, clubs_urls, clubs_names, clubs_countries, clubs_squads, clubs_market_values)
        # The columns come from independent xpath queries; a length mismatch would shift values between clubs.
        if len({len(column) for column in columns}) != 1:
            raise UpstreamError(
                502,
                self.URL,
                f"Unexpected search results page: {len(clubs_ids)} ids, {len(clubs_urls)} urls, "
                f"{len(clubs_names)} names, {len(clubs_countries)} countries, {len(clubs_squads)} squads, "
                f"{len(clubs_market_values)} market values",
            )

        return [
            {
                "id": idx,
                "url": url,
                "name": name,
                "country": country,
                "squad": squad,
                "marketValue": market_value,
            }
            for idx, url, name, country, squad, market_value in zip(
                clubs_ids,
                clubs_urls,
                clubs_names,
                clubs_countries,
                clubs_squads,
                clubs_market_values,
                strict=True,
            )
        ]

    def search_clubs(self) -> dict:
        """
        Perform a search for football clubs on Transfermarkt and retrieve search results.

        Returns:
            dict: A dictionary containing the search query, current page number, last page number,
                search results, and the timestamp of when the search was conducted.
        """
        self.response["query"] = self.query
        self.response["pageNumber"] = self.page_number
        self.response["lastPageNumber"] = self.get_last_page_number(Clubs.Search.BASE)
        self.response["results"] = self.__parse_search_results()

        return self.response
