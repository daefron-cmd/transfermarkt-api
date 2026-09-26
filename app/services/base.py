from dataclasses import dataclass, field
from typing import Any

import lxml.html
from fastapi import HTTPException

from app.utils.utils import trim
from app.utils.xpath import Pagination


@dataclass
class TransfermarktBase:
    """
    Base class for parsing Transfermarkt web pages. Fetching happens in each service's `fetch` classmethod.

    Args:
        URL (str): The URL the page was fetched from (used in error details).
        page (HtmlElement): The parsed web page content.
    Attributes:
        response (dict): A dictionary to store the response data.
    """

    URL: str
    page: lxml.html.HtmlElement
    response: dict = field(default_factory=dict, init=False)

    def raise_exception_if_not_found(self, xpath: str) -> None:
        """
        Raise an exception if the specified XPath does not yield any results on the web page.

        Args:
            xpath (str): The XPath expression to query elements on the page.

        Raises:
            HTTPException: If the specified XPath query does not yield any results, indicating an invalid request.
        """
        if not self.get_text_by_xpath(xpath):
            raise HTTPException(status_code=404, detail=f"Invalid request (url: {self.URL})")

    def get_list_by_xpath(self, xpath: str, remove_empty: bool | None = True) -> list:
        """
        Extract a list of elements from the web page using the specified XPath expression.

        Args:
            xpath (str): The XPath expression to query elements on the page.
            remove_empty (bool, optional): If True, remove empty or whitespace-only elements from
                the list. Default is True.

        Returns:
            list: A list of elements extracted from the web page based on the XPath query.
                If remove_empty is True, empty or whitespace-only elements are filtered out.
        """
        elements: Any = self.page.xpath(xpath)
        if remove_empty:
            return [trim(e) for e in elements if trim(e)]
        return [trim(e) for e in elements]

    def get_text_by_xpath(
        self,
        xpath: str,
        pos: int = 0,
        iloc: int | None = None,
        iloc_from: int | None = None,
        iloc_to: int | None = None,
        join_str: str | None = None,
    ) -> str | None:
        """
        Extract text content from the web page using the specified XPath expression.

        Args:
            xpath (str): The XPath expression to query elements on the page.
            pos (int, optional): Index of the element to extract if multiple elements match the
                XPath. Default is 0.
            iloc (int, optional): Extract a single element by index, used as an alternative to 'pos'.
            iloc_from (int, optional): Extract a range of elements starting from the specified
                index (inclusive).
            iloc_to (int, optional): Extract a range of elements up to the specified
                index (exclusive).
            join_str (str, optional): If provided, join multiple text elements into a single string
                using this separator.

        Returns:
            Optional[str]: The extracted text content from the web page based on the XPath query and
                optional parameters. If no matching element is found, None is returned.
        """
        element: Any = self.page.xpath(xpath)

        if not element:
            return None

        if isinstance(element, list):
            element = [trim(e) for e in element if trim(e)]

        if isinstance(iloc, int):
            element = element[iloc]

        if isinstance(iloc_from, int) and isinstance(iloc_to, int):
            element = element[iloc_from:iloc_to]
        elif isinstance(iloc_to, int):
            element = element[:iloc_to]
        elif isinstance(iloc_from, int):
            element = element[iloc_from:]

        if isinstance(join_str, str):
            return join_str.join([trim(e) for e in element])

        try:
            return trim(element[pos])
        except IndexError:
            return None

    def get_last_page_number(self, xpath_base: str = "") -> int:
        """
        Retrieve the last page number for a paginated result based on the provided base XPath.

        Args:
            xpath_base (str): The base XPath for extracting page number information.

        Returns:
            int: The last page number for search results. Returns 1 if no page numbers are found.
        """

        for xpath in [Pagination.PAGE_NUMBER_LAST, Pagination.PAGE_NUMBER_ACTIVE]:
            url_page = self.get_text_by_xpath(xpath_base + xpath)
            if url_page:
                return int(url_page.split("=")[-1].split("/")[-1])
        return 1
