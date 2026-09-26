from dataclasses import dataclass
from typing import ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url
from app.utils.xpath import Competitions


@dataclass
class TransfermarktCompetitionClubs(TransfermarktBase):
    """
    A class for retrieving and parsing the list of football clubs in a specific competition on Transfermarkt.

    Args:
        competition_id (str): The unique identifier of the competition.

    Attributes:
        URL_TEMPLATE (str): The URL template for the competition's page on Transfermarkt.
    """

    competition_id: str
    # {season} is "?saison_id=<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/startseite/wettbewerb/{competition_id}/plus/{season}"

    def __post_init__(self) -> None:
        """Validate that the page is a competition page."""
        self.raise_exception_if_not_found(xpath=Competitions.Profile.NAME)

    @classmethod
    def from_bytes(cls, html: bytes, *, competition_id: str, season_id: str | None = None) -> Self:
        """Build the service from an already fetched competition page."""
        season = f"?saison_id={season_id}" if season_id else ""
        url = cls.URL_TEMPLATE.format(competition_id=competition_id, season=season)
        return cls(
            URL=url,
            page=parse_html(url, html),
            competition_id=competition_id,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, competition_id: str, season_id: str | None = None) -> Self:
        """Fetch and parse the competition's page for a season (the current one if not given)."""
        season = f"?saison_id={season_id}" if season_id else ""
        response = await client.get(cls.URL_TEMPLATE.format(competition_id=competition_id, season=season))
        return cls.from_bytes(response.content, competition_id=competition_id, season_id=season_id)

    def __parse_competition_clubs(self) -> list:
        """
        Parse the competition's page and extract information about the football clubs participating
            in the competition.

        Returns:
            list: A list of dictionaries, where each dictionary contains information about a
                football club in the competition, including the club's unique identifier and name.

        Raises:
            UpstreamError: If the club links and names do not have the same length (the page layout changed).
        """
        urls = self.get_list_by_xpath(Competitions.Clubs.URLS)
        names = self.get_list_by_xpath(Competitions.Clubs.NAMES)
        ids = [extract_from_url(url) for url in urls]
        if len(ids) != len(names):
            raise UpstreamError(502, self.URL, f"Unexpected competition clubs page: {len(ids)} ids, {len(names)} names")

        return [{"id": idx, "name": name} for idx, name in zip(ids, names, strict=True)]

    def get_competition_clubs(self) -> dict:
        """
        Retrieve and parse the list of football clubs participating in a specific competition.

        Returns:
            dict: A dictionary containing the competition's unique identifier, name, season identifier, list of clubs
                  participating in the competition, and the timestamp of when the data was last updated.
        """
        self.response["id"] = self.competition_id
        self.response["name"] = self.get_text_by_xpath(Competitions.Profile.NAME)
        self.response["seasonId"] = extract_from_url(
            self.get_text_by_xpath(Competitions.Profile.URL),
            "season_id",
        )
        self.response["clubs"] = self.__parse_competition_clubs()

        return self.response
