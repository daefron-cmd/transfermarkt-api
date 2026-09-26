from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from fastapi import HTTPException

from app.http import TransfermarktClient, UpstreamError
from app.tmapi import TMAPI_URL, get_attributes, lookup_names, names_by_id, tmapi_data

GOALKEEPER_CATEGORY = "Goalkeeper"
PLAYED = "played"


def _get(row: dict[str, Any], *path: str) -> Any:
    """Follow `path` through nested dicts; None if a step is missing."""
    value: Any = row
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


@dataclass
class TransfermarktPlayerStats:
    """
    A player's per-season, per-competition, per-club statistics, aggregated from the per-game rows of tmapi's
    /player/{id}/performance-game (an unofficial JSON API; see app.tmapi).

    Rows are grouped by (gameInformation.seasonId, gameInformation.competitionId, clubsInformation.club.clubId), the
    club the player was fielded for (a national team for international games, not generalStatistics.primaryClubId,
    which is the contract club). Every group with at least one row is returned, including groups where the player
    never played. A game is "played" when generalStatistics.participationState is "played" (other values seen:
    "in squad", "not in squad", "injured", "absent"); only played games are aggregated:

    - appearances: number of played games
    - minutesPlayed: sum of playingTimeStatistics.playedMinutes
    - goals: sum of goalStatistics.goalsScoredTotal
    - assists: sum of goalStatistics.assists
    - ownGoals: sum of goalStatistics.ownGoalsScored
    - penaltyGoals: sum of goalStatistics.penaltyShooterGoalsScored
    - yellowCards: sum of cardStatistics.yellowCardNet (a yellow that became a second yellow is not counted)
    - secondYellowCards: games with a cardStatistics.yellowRedCard entry
    - redCards: games with a cardStatistics.redCard entry (straight red)
    - goalsConceded: sum of goalStatistics.opponentGoalsOnThePitch over goalkeeper games
    - cleanSheets: goalkeeper games with clubsInformation.club.opponentGoalsTotal == 0 (the whole-match score)

    A goalkeeper game is a played game whose generalStatistics.positionId has category "Goalkeeper" in the
    /attributes positions table or, when that positionId is 0, None or not in the table, a played game of a player
    whose main position (data.attributes.positionId of /player/{id}) has category "Goalkeeper".
    goalsConceded and cleanSheets are None for a group without goalkeeper games. seasonName is
    gameInformation.season.display ("24/25" for club seasons, "2025" for calendar-year national-team seasons).

    Args:
        player_id (str): The unique identifier of the player.
        performance (list): The per-game rows (data.performance).
        competition_names (dict): Competition id to name.
        club_names (dict): Club id to name.
        position_categories (dict): Position id to category ("Goalkeeper", "Defender", ...).
        main_position_id (int, optional): The player's main position id.
        season_id (str, optional): Only aggregate games of this season.
    """

    player_id: str
    performance: list[dict[str, Any]]
    competition_names: dict[str, str]
    club_names: dict[str, str]
    position_categories: dict[int, str]
    main_position_id: int | None
    season_id: str | None = None
    response: dict = field(default_factory=dict, init=False)
    URL_TEMPLATE: ClassVar[str] = TMAPI_URL + "/player/{player_id}/performance-game"
    URL_PLAYER: ClassVar[str] = TMAPI_URL + "/player/{player_id}"
    URL_ATTRIBUTES: ClassVar[str] = TMAPI_URL + "/attributes"

    @classmethod
    def parse_performance(cls, content: bytes, *, player_id: str, season_id: str | None = None) -> list[dict]:
        """
        Unwrap and validate the performance-game response and return its rows, filtered to `season_id` if given.

        Raises:
            HTTPException: 404 if tmapi reports the player as not found or has no games for them.
            UpstreamError: If the response does not have the expected shape.
        """
        url = cls.URL_TEMPLATE.format(player_id=player_id)
        try:
            data = tmapi_data(url, content, failure_status=404)
        except UpstreamError as e:
            if e.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Invalid request (url: {url})") from e
            raise
        performance = data.get("performance") if isinstance(data, dict) else None
        if not isinstance(performance, list):
            raise UpstreamError(502, url, "Unexpected tmapi response: no performance list")
        if not performance:
            raise HTTPException(status_code=404, detail=f"Invalid request (url: {url})")
        for row in performance:
            cls._validate_row(url, row)
        if season_id is not None:
            performance = [row for row in performance if str(row["gameInformation"]["seasonId"]) == season_id]
        return performance

    @staticmethod
    def _validate_row(url: str, row: Any) -> None:
        """Check the fields the aggregation relies on; a changed upstream shape must fail loudly, not miscount."""
        required: list[tuple[tuple[str, ...], type | tuple[type, ...]]] = [
            (("gameInformation", "seasonId"), int),
            (("gameInformation", "competitionId"), str),
            (("gameInformation", "season", "display"), str),
            (("clubsInformation", "club", "clubId"), str),
            (("statistics", "generalStatistics", "participationState"), str),
        ]
        if _get(row, "statistics", "generalStatistics", "participationState") == PLAYED:
            required += [
                (("statistics", "generalStatistics", "positionId"), (int, type(None))),
                (("statistics", "playingTimeStatistics", "playedMinutes"), int),
                (("statistics", "goalStatistics", "goalsScoredTotal"), int),
                (("statistics", "goalStatistics", "assists"), int),
                (("statistics", "goalStatistics", "ownGoalsScored"), int),
                (("statistics", "goalStatistics", "penaltyShooterGoalsScored"), int),
                (("statistics", "goalStatistics", "opponentGoalsOnThePitch"), int),
                (("clubsInformation", "club", "opponentGoalsTotal"), int),
                (("statistics", "cardStatistics", "yellowCardNet"), int),
            ]
        for path, expected in required:
            value = _get(row, *path)
            if not isinstance(value, expected) or isinstance(value, bool):
                game_id = _get(row, "gameInformation", "gameId")
                raise UpstreamError(
                    502, url, f"Unexpected tmapi response: {'.'.join(path)}={value!r} in game {game_id}"
                )

    @staticmethod
    def parse_position_categories(content: bytes) -> dict[int, str]:
        """Return position id to category from the /attributes response."""
        url = TransfermarktPlayerStats.URL_ATTRIBUTES
        return TransfermarktPlayerStats._position_categories(url, tmapi_data(url, content))

    @staticmethod
    def _position_categories(url: str, attributes: Any) -> dict[int, str]:
        positions = attributes.get("positions") if isinstance(attributes, dict) else None
        if not isinstance(positions, list) or not all(
            isinstance(p, dict) and isinstance(p.get("id"), int) and isinstance(p.get("category"), str)
            for p in positions
        ):
            raise UpstreamError(
                502, url, "Unexpected tmapi response: attributes.positions is not a list of {id, category, ...}"
            )
        categories = {p["id"]: p["category"] for p in positions}
        if GOALKEEPER_CATEGORY not in categories.values():
            raise UpstreamError(502, url, "Unexpected tmapi response: no position with category Goalkeeper")
        return categories

    @classmethod
    def parse_main_position(cls, content: bytes, *, player_id: str) -> int | None:
        """Return the player's main position id (data.attributes.positionId) from the /player/{id} response."""
        url = cls.URL_PLAYER.format(player_id=player_id)
        data = tmapi_data(url, content)
        attributes = data.get("attributes") if isinstance(data, dict) else None
        position_id = attributes.get("positionId") if isinstance(attributes, dict) else None
        if not isinstance(attributes, dict) or not isinstance(position_id, int | None) or isinstance(position_id, bool):
            raise UpstreamError(502, url, f"Unexpected tmapi response: attributes.positionId={position_id!r}")
        return position_id

    def _is_goalkeeper_game(self, position_id: int | None) -> bool:
        """Whether a played game counts for goalkeeper stats; see the class docstring."""
        if position_id in self.position_categories:
            return self.position_categories[position_id] == GOALKEEPER_CATEGORY
        if self.main_position_id is None:
            return False
        return self.position_categories.get(self.main_position_id) == GOALKEEPER_CATEGORY

    @staticmethod
    def competition_ids(performance: list[dict]) -> set[str]:
        return {row["gameInformation"]["competitionId"] for row in performance}

    @staticmethod
    def club_ids(performance: list[dict]) -> set[str]:
        return {row["clubsInformation"]["club"]["clubId"] for row in performance}

    @classmethod
    def from_bytes(
        cls,
        performance: bytes,
        *,
        player: bytes,
        attributes: bytes,
        competitions: Sequence[tuple[str, bytes]],
        clubs: Sequence[tuple[str, bytes]],
        player_id: str,
        season_id: str | None = None,
    ) -> Self:
        """
        Build the service from already fetched responses: the performance-game, /player/{id} and /attributes bodies
        and the (url, body) pairs of the competitions and clubs batch lookups.
        """
        rows = cls.parse_performance(performance, player_id=player_id, season_id=season_id)
        return cls(
            player_id=player_id,
            performance=rows,
            competition_names={k: v for url, body in competitions for k, v in names_by_id(url, body).items()},
            club_names={k: v for url, body in clubs for k, v in names_by_id(url, body).items()},
            position_categories=cls.parse_position_categories(attributes),
            main_position_id=cls.parse_main_position(player, player_id=player_id),
            season_id=season_id,
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str, season_id: str | None = None) -> Self:
        """
        Fetch the player's games (first, so an unknown player is a 404 before anything else is requested), then their
        main position, the position table and the names of their competitions and clubs.
        """
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id))
        rows = cls.parse_performance(response.content, player_id=player_id, season_id=season_id)
        player = await client.get(cls.URL_PLAYER.format(player_id=player_id))
        attributes = await get_attributes(client)
        return cls(
            player_id=player_id,
            performance=rows,
            competition_names=await lookup_names(client, "competitions", cls.competition_ids(rows)),
            club_names=await lookup_names(client, "clubs", cls.club_ids(rows)),
            position_categories=cls._position_categories(cls.URL_ATTRIBUTES, attributes),
            main_position_id=cls.parse_main_position(player.content, player_id=player_id),
            season_id=season_id,
        )

    def __parse_player_stats(self) -> list[dict]:
        """Aggregate the per-game rows into one entry per (season, competition, club) as described on the class."""
        groups: dict[tuple[int, str, str], dict[str, Any]] = {}
        for row in self.performance:
            game = row["gameInformation"]
            club_id = row["clubsInformation"]["club"]["clubId"]
            key = (game["seasonId"], game["competitionId"], club_id)
            if key not in groups:
                groups[key] = {
                    "seasonId": str(game["seasonId"]),
                    "seasonName": game["season"]["display"],
                    "competitionId": game["competitionId"],
                    "competitionName": self.competition_names[game["competitionId"]],
                    "clubId": club_id,
                    "clubName": self.club_names[club_id],
                    "appearances": 0,
                    "goals": 0,
                    "assists": 0,
                    "ownGoals": 0,
                    "penaltyGoals": 0,
                    "yellowCards": 0,
                    "secondYellowCards": 0,
                    "redCards": 0,
                    "minutesPlayed": 0,
                    "goalsConceded": None,
                    "cleanSheets": None,
                }
            stats = row["statistics"]
            if stats["generalStatistics"]["participationState"] != PLAYED:
                continue
            group = groups[key]
            goal_stats, card_stats = stats["goalStatistics"], stats["cardStatistics"]
            group["appearances"] += 1
            group["minutesPlayed"] += stats["playingTimeStatistics"]["playedMinutes"]
            group["goals"] += goal_stats["goalsScoredTotal"]
            group["assists"] += goal_stats["assists"]
            group["ownGoals"] += goal_stats["ownGoalsScored"]
            group["penaltyGoals"] += goal_stats["penaltyShooterGoalsScored"]
            group["yellowCards"] += card_stats["yellowCardNet"]
            group["secondYellowCards"] += card_stats.get("yellowRedCard") is not None
            group["redCards"] += card_stats.get("redCard") is not None
            if self._is_goalkeeper_game(stats["generalStatistics"]["positionId"]):
                clean_sheet = row["clubsInformation"]["club"]["opponentGoalsTotal"] == 0
                group["goalsConceded"] = (group["goalsConceded"] or 0) + goal_stats["opponentGoalsOnThePitch"]
                group["cleanSheets"] = (group["cleanSheets"] or 0) + clean_sheet

        stats_rows = sorted(groups.values(), key=lambda s: (s["competitionName"], s["clubName"]))
        return sorted(stats_rows, key=lambda s: int(s["seasonId"]), reverse=True)

    def get_player_stats(self) -> dict:
        """
        Retrieve the player's aggregated statistics.

        Returns:
            dict: The player's unique identifier and one statistics entry per (season, competition, club), sorted by
                season (newest first), then competition name and club name.
        """
        self.response["id"] = self.player_id
        self.response["stats"] = self.__parse_player_stats()

        return self.response
