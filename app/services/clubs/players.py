import re
from dataclasses import dataclass, field
from typing import ClassVar, Self

from fastapi import HTTPException

from app.http import TransfermarktClient, UpstreamError
from app.services.base import TransfermarktBase, parse_html
from app.utils.regex import REGEX_DOB, REGEX_FEE_LABEL
from app.utils.utils import extract_from_url, parse_int, safe_regex, trim
from app.utils.xpath import Clubs

# The squad table's column headers on a club's current season, a club's past season and a national team's page.
CLUB_HEADERS = (
    "#",
    "Player",
    "Date of birth/Age",
    "Nat.",
    "Height",
    "Foot",
    "Joined",
    "Signed from",
    "Contract",
    "Market value",
)
PAST_CLUB_HEADERS = (
    "#",
    "Player",
    "Date of birth/Age",
    "Nat.",
    "Current club",
    "Height",
    "Foot",
    "Joined",
    "Signed from",
    "Market value",
)
NATIONAL_HEADERS = (
    "#",
    "Player",
    "Date of birth/Age",
    "Club",
    "Height",
    "Foot",
    "International matches",
    "Goals",
    "Debut",
    "Market value",
)
# The national team columns where "-" means none (0) rather than unknown.
NATIONAL_COUNTS = ("International matches", "Goals")


@dataclass
class TransfermarktClubPlayers(TransfermarktBase):
    """
    A class for retrieving and parsing the players of a football club from Transfermarkt.

    Args:
        club_id (str): The unique identifier of the football club.
        season_id (str): The unique identifier of the season.

    Attributes:
        URL_TEMPLATE (str): The URL template for the club's players page on Transfermarkt.
        past (bool): Whether the page lists a club's past season squad.
        national (bool): Whether the page lists a national team's squad.
    """

    club_id: str
    season_id: str | None = None
    past: bool = field(default=False, init=False)
    national: bool = field(default=False, init=False)
    # {season} is "/saison_id/<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/kader/verein/{club_id}{season}/plus/1"

    def __post_init__(self) -> None:
        """Validate that the page is the club's squad page, then resolve the season and the table layout."""
        self.raise_exception_if_not_found(xpath=Clubs.Players.CLUB_NAME)
        # An unknown club redirects to another page (e.g. "Most valuable clubs"), which has no squad tab of this club.
        if f"/kader/verein/{self.club_id}/" not in (self.get_text_by_xpath(Clubs.Players.SEASON_URL) or ""):
            raise HTTPException(status_code=404, detail=f"Invalid request (url: {self.URL})")
        self.__update_season_id()
        self.__update_layout()

    @classmethod
    def from_bytes(cls, html: bytes, *, club_id: str, season_id: str | None = None) -> Self:
        """Build the service from an already fetched squad page."""
        season = f"/saison_id/{season_id}" if season_id else ""
        url = cls.URL_TEMPLATE.format(club_id=club_id, season=season)
        return cls(
            URL=url,
            page=parse_html(url, html),
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

    def __update_layout(self) -> None:
        """
        Set the past and national flags from the squad table's column headers.

        Raises:
            UpstreamError: If the headers are not those of a club's current or past season or of a national team.
        """
        headers = tuple(self.get_list_by_xpath(Clubs.Players.HEADERS))
        if headers not in (CLUB_HEADERS, PAST_CLUB_HEADERS, NATIONAL_HEADERS):
            raise UpstreamError(502, self.URL, f"Unexpected squad table headers {list(headers)}")
        self.past = headers == PAST_CLUB_HEADERS
        self.national = headers == NATIONAL_HEADERS

    def __parse_national_players(self) -> list[dict]:
        """
        Parse a national team's squad row by row; "-" (no caps or goals) in the international matches and goals
        columns is 0.

        Raises:
            UpstreamError: If a row does not have one cell per column or one player link.
        """
        players = []
        for number, row in enumerate(self.page.xpath(Clubs.Players.ROWS), start=1):
            cells = row.xpath(Clubs.Players.National.CELLS)
            urls = row.xpath(Clubs.Players.National.URL)
            if len(cells) != len(NATIONAL_HEADERS) or len(urls) != 1:
                raise UpstreamError(
                    502,
                    self.URL,
                    f"Squad row {number} has {len(cells)} cells for {len(NATIONAL_HEADERS)} columns"
                    f" and {len(urls)} player links",
                )
            text = dict(
                zip(NATIONAL_HEADERS, (trim(cell.xpath(Clubs.Players.National.TEXT)) for cell in cells), strict=True)
            )
            dob_age = text["Date of birth/Age"]
            matches, goals = (0 if text[column] == "-" else parse_int(text[column]) for column in NATIONAL_COUNTS)
            players.append(
                {
                    "id": extract_from_url(urls[0]),
                    "name": trim(row.xpath(Clubs.Players.National.NAME)),
                    "position": trim(cells[1].xpath(Clubs.Players.National.POSITION)),
                    "dateOfBirth": safe_regex(dob_age, REGEX_DOB, "dob"),
                    "age": safe_regex(dob_age, REGEX_DOB, "age"),
                    "currentClub": next(iter(cells[3].xpath(Clubs.Players.National.CLUB)), None),
                    "height": text["Height"],
                    "foot": text["Foot"] or None,
                    "internationalMatches": matches,
                    "internationalGoals": goals,
                    "debut": text["Debut"],
                    "marketValue": text["Market value"],
                    "status": "; ".join(row.xpath(Clubs.Players.STATUSES)),
                }
            )
        return players

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
        self.response["players"] = self.__parse_national_players() if self.national else self.__parse_club_players()

        return self.response
