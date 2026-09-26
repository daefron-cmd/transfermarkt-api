from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.tmapi import TMAPI_URL, check_fields, lookup_names, names_by_id, parse_competition, tmapi_data

CLUB_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("clubId", str),
    ("ranking.current", int),
    ("ranking.previous", (int, type(None))),
    ("game.totalCount", int),
    ("game.winCount", int),
    ("game.drawCount", int),
    ("game.lossCount", int),
    ("game.points", int),
    ("game.pointsMinus", (int, type(None))),
    ("goal.totalCount", int),
    ("goal.concededCount", int),
    ("goal.differenceCount", int),
    ("positioning.description", str),
]


@dataclass
class TransfermarktCompetitionTable:
    """
    A competition's league table(s) for a season, from tmapi's /competition/{id}/table (an unofficial JSON API; see
    app.tmapi).

    A league has one table; a competition with a group stage has one table per group ("Group A", ...); a knockout cup
    has none. Rows are sorted by position (tmapi's own order; the sort is stable) and map a tmapi club row as
    follows: position: ranking.current, previousPosition: ranking.previous, played/won/drawn/lost:
    game.totalCount/winCount/drawCount/lossCount, goalsFor/goalsAgainst/goalDifference:
    goal.totalCount/concededCount/differenceCount, points: game.points, pointsDeducted: game.pointsMinus, zone:
    positioning.description (None when "").

    Args:
        competition_id (str): The unique identifier of the competition.
        competition_name (str): The competition's name.
        season_id (str): The season of the table (the requested one, or the competition's current season).
        tables (list): The validated tmapi tables (data.tables).
        club_names (dict): Club id to name.
    """

    competition_id: str
    competition_name: str
    season_id: str
    tables: list[dict[str, Any]]
    club_names: dict[str, str]
    response: dict = field(default_factory=dict, init=False)
    URL_COMPETITION: ClassVar[str] = TMAPI_URL + "/competition/{competition_id}"
    # {season} is "?season=<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = TMAPI_URL + "/competition/{competition_id}/table{season}"

    @classmethod
    def table_url(cls, competition_id: str, season_id: str | None) -> str:
        season = f"?season={season_id}" if season_id else ""
        return cls.URL_TEMPLATE.format(competition_id=competition_id, season=season)

    @classmethod
    def parse_tables(cls, content: bytes, *, competition_id: str, season_id: str | None = None) -> list[dict]:
        """
        Unwrap and validate the table response and return its tables (possibly empty).

        Raises:
            UpstreamError: If the response does not have the expected shape.
        """
        url = cls.table_url(competition_id, season_id)
        data = tmapi_data(url, content)
        tables = data.get("tables") if isinstance(data, dict) else None
        if not isinstance(tables, list):
            raise UpstreamError(502, url, "Unexpected tmapi response: no tables list")
        for i, table in enumerate(tables):
            check_fields(url, table, [("meta.name", str), ("clubs", list)], f"table {i}")
            for club in table["clubs"]:
                where = f"table {table['meta']['name']!r} club {club.get('clubId') if isinstance(club, dict) else None}"
                check_fields(url, club, CLUB_FIELDS, where)
        return tables

    @staticmethod
    def club_ids(tables: list[dict]) -> set[str]:
        return {club["clubId"] for table in tables for club in table["clubs"]}

    @classmethod
    def from_bytes(
        cls,
        table: bytes,
        *,
        competition: bytes,
        clubs: Sequence[tuple[str, bytes]],
        competition_id: str,
        season_id: str | None = None,
    ) -> Self:
        """
        Build the service from already fetched responses: the table and /competition/{id} bodies and the (url, body)
        pairs of the clubs batch lookups.
        """
        name, current_season_id = parse_competition(
            cls.URL_COMPETITION.format(competition_id=competition_id), competition
        )
        return cls(
            competition_id=competition_id,
            competition_name=name,
            season_id=season_id or str(current_season_id),
            tables=cls.parse_tables(table, competition_id=competition_id, season_id=season_id),
            club_names={k: v for url, body in clubs for k, v in names_by_id(url, body).items()},
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, competition_id: str, season_id: str | None = None) -> Self:
        """
        Fetch the competition (first, so an unknown competition is a 404 before anything else is requested), its table
        for the season (the current one if not given) and the names of its clubs.
        """
        response = await client.get(cls.URL_COMPETITION.format(competition_id=competition_id))
        name, current_season_id = parse_competition(response.url, response.content)
        response = await client.get(cls.table_url(competition_id, season_id))
        tables = cls.parse_tables(response.content, competition_id=competition_id, season_id=season_id)
        return cls(
            competition_id=competition_id,
            competition_name=name,
            season_id=season_id or str(current_season_id),
            tables=tables,
            club_names=await lookup_names(client, "clubs", cls.club_ids(tables)),
        )

    def __parse_row(self, club: dict) -> dict:
        game, goal = club["game"], club["goal"]
        return {
            "clubId": club["clubId"],
            "clubName": self.club_names[club["clubId"]],
            "position": club["ranking"]["current"],
            "previousPosition": club["ranking"]["previous"],
            "played": game["totalCount"],
            "won": game["winCount"],
            "drawn": game["drawCount"],
            "lost": game["lossCount"],
            "goalsFor": goal["totalCount"],
            "goalsAgainst": goal["concededCount"],
            "goalDifference": goal["differenceCount"],
            "points": game["points"],
            "pointsDeducted": game["pointsMinus"],
            "zone": club["positioning"]["description"] or None,
        }

    def get_competition_table(self) -> dict:
        """
        Retrieve the competition's table(s).

        Returns:
            dict: The competition's id, name and season and its tables, each with a name and rows as described on the
                class.
        """
        self.response["id"] = self.competition_id
        self.response["name"] = self.competition_name
        self.response["seasonId"] = self.season_id
        self.response["tables"] = [
            {
                "name": table["meta"]["name"],
                "rows": sorted((self.__parse_row(club) for club in table["clubs"]), key=lambda row: row["position"]),
            }
            for table in self.tables
        ]
        return self.response
