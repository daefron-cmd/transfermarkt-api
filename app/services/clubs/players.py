import re
from dataclasses import dataclass, field
from typing import ClassVar, Self

import lxml.html

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase
from app.utils.regex import REGEX_DOB, REGEX_FEE_LABEL
from app.utils.utils import extract_from_url, safe_regex
from app.utils.xpath import Clubs


@dataclass
class TransfermarktClubPlayers(TransfermarktBase):
    """
    A class for retrieving and parsing the players of a football club from Transfermarkt.

    Args:
        club_id (str): The unique identifier of the football club.
        season_id (str): The unique identifier of the season.

    Attributes:
        URL_TEMPLATE (str): The URL template for the club's players page on Transfermarkt.
        past (bool): Whether the page lists a past season's squad.
    """

    club_id: str
    season_id: str | None = None
    past: bool = field(default=False, init=False)
    # {season} is "/saison_id/<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/kader/verein/{club_id}{season}/plus/1"

    def __post_init__(self) -> None:
        """Validate that the page is a club squad page, then resolve the season and the past-season flag."""
        self.raise_exception_if_not_found(xpath=Clubs.Players.CLUB_NAME)
        self.__update_season_id()
        self.__update_past_flag()

    @classmethod
    def from_bytes(cls, html: bytes, *, club_id: str, season_id: str | None = None) -> Self:
        """Build the service from an already fetched squad page."""
        season = f"/saison_id/{season_id}" if season_id else ""
        return cls(
            URL=cls.URL_TEMPLATE.format(club_id=club_id, season=season),
            page=lxml.html.document_fromstring(html),
            club_id=club_id,
            season_id=season_id,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, club_id: str, season_id: str | None = None) -> Self:
        """Fetch and parse the club's squad page for a season (the current one if not given)."""
        season = f"/saison_id/{season_id}" if season_id else ""
        response = await client.get(cls.URL_TEMPLATE.format(club_id=club_id, season=season))
        return cls.from_bytes(response.content, club_id=club_id, season_id=season_id)

    def __update_season_id(self):
        """Update the season ID if it's not provided by extracting it from the website."""
        if self.season_id is None:
            self.season_id = extract_from_url(self.get_text_by_xpath(Clubs.Players.SEASON_URL), "season_id")

    def __update_past_flag(self) -> None:
        """Check if the season is the current or if it's a past one and update the flag accordingly."""
        self.past = "Current club" in self.get_list_by_xpath(Clubs.Players.PAST_FLAG)

    def __parse_club_players(self) -> list[dict]:
        """
        Parse player information from the webpage and return a list of dictionaries, each representing a player.

        Returns:
            list[dict]: A list of player information dictionaries.

        Raises:
            UpstreamError: If a column does not have one value per player row (the page layout changed).
        """
        page_nationalities = self.page.xpath(Clubs.Players.PAGE_NATIONALITIES)
        page_players_infos = self.page.xpath(Clubs.Players.PAGE_INFOS)
        page_players_signed_from = self.page.xpath(
            Clubs.Players.Past.PAGE_SIGNED_FROM if self.past else Clubs.Players.Present.PAGE_SIGNED_FROM,
        )
        page_players_joined_on = self.page.xpath(
            Clubs.Players.Past.PAGE_JOINED_ON if self.past else Clubs.Players.Present.PAGE_JOINED_ON,
        )
        players_ids = [extract_from_url(url) for url in self.get_list_by_xpath(Clubs.Players.URLS)]
        players_names = self.get_list_by_xpath(Clubs.Players.NAMES)
        players_positions = self.get_list_by_xpath(Clubs.Players.POSITIONS)
        players_dobs = [
            safe_regex(dob_age, REGEX_DOB, "dob") for dob_age in self.get_list_by_xpath(Clubs.Players.DOB_AGE)
        ]
        players_ages = [
            safe_regex(dob_age, REGEX_DOB, "age") for dob_age in self.get_list_by_xpath(Clubs.Players.DOB_AGE)
        ]
        players_nationalities = [nationality.xpath(Clubs.Players.NATIONALITIES) for nationality in page_nationalities]
        players_current_club = (
            self.get_list_by_xpath(Clubs.Players.Past.CURRENT_CLUB) if self.past else [None] * len(players_ids)
        )
        players_heights = self.get_list_by_xpath(
            Clubs.Players.Past.HEIGHTS if self.past else Clubs.Players.Present.HEIGHTS,
        )
        players_foots = self.get_list_by_xpath(
            Clubs.Players.Past.FOOTS if self.past else Clubs.Players.Present.FOOTS,
            remove_empty=False,
        )
        players_joined_on = ["; ".join(e.xpath(Clubs.Players.JOINED_ON)) for e in page_players_joined_on]
        signed_from_titles = [(e.xpath(Clubs.Players.SIGNED_FROM_TITLE) or [""])[0] for e in page_players_signed_from]
        players_signed_from = [
            "; ".join(e.xpath(Clubs.Players.SIGNED_FROM)) or title.rsplit(": ", 1)[0]
            for e, title in zip(page_players_signed_from, signed_from_titles, strict=True)
        ]
        players_signed_from_fees = [
            re.sub(REGEX_FEE_LABEL, "", title.rsplit(": ", 1)[1]) if ": " in title else None
            for title in signed_from_titles
        ]
        players_contracts = (
            [None] * len(players_ids) if self.past else self.get_list_by_xpath(Clubs.Players.Present.CONTRACTS)
        )
        players_marketvalues = self.get_list_by_xpath(Clubs.Players.MARKET_VALUES)
        players_statuses = ["; ".join(e.xpath(Clubs.Players.STATUSES)) for e in page_players_infos if e is not None]

        columns = {
            "id": players_ids,
            "name": players_names,
            "position": players_positions,
            "dateOfBirth": players_dobs,
            "age": players_ages,
            "nationality": players_nationalities,
            "currentClub": players_current_club,
            "height": players_heights,
            "foot": players_foots,
            "joinedOn": players_joined_on,
            "signedFrom": players_signed_from,
            "signedFromFee": players_signed_from_fees,
            "contract": players_contracts,
            "marketValue": players_marketvalues,
            "status": players_statuses,
        }
        # The columns come from independent xpath queries; a length mismatch would shift values between players.
        rows = len(self.page.xpath(Clubs.Players.ROWS))
        for name, values in columns.items():
            if len(values) != rows:
                raise UpstreamError(502, self.URL, f"Squad column {name!r} has {len(values)} values for {rows} rows")
        return [dict(zip(columns, values, strict=True)) for values in zip(*columns.values(), strict=True)]

    def get_club_players(self) -> dict:
        """
        Retrieve and parse player information for the specified football club.

        Returns:
            dict: A dictionary containing the club's unique identifier, player information, and the timestamp of when
                  the data was last updated.
        """
        self.response["id"] = self.club_id
        self.response["seasonId"] = self.season_id
        self.response["players"] = self.__parse_club_players()

        return self.response
