from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.services.games import (
    URL_GAME,
    competition_group_names,
    fetch_group_names,
    fetch_shootout_scores,
    parse_game,
    parse_shootout_score,
    validate_game,
)
from app.tmapi import TMAPI_URL, lookup_names, names_by_id, parse_club, tmapi_data

GAME_RESULTS = ("W", "D", "L")


@dataclass
class TransfermarktClubFixtures:
    """
    A club's games in a season across all competitions, from tmapi's /club/{id}/fixtures (an unofficial JSON API; see
    app.tmapi). Games are mapped as described in app.services.games, sorted by date and id, and extended with
    venue: "home" if the club is the home club, else "away"; result: score.gameResult ("W", "D" or "L", from the
    club's point of view; the shootout winner's "W" after a shootout) of a finished game, else None.

    Args:
        club_id (str): The unique identifier of the club.
        club_name (str): The club's name.
        season_id (str | None): The requested season, or None for the current season.
        games (list): The validated tmapi games.
        club_names (dict): Club id to name.
        competition_names (dict): Competition id to name.
        group_names (dict): Competition group id to name, for games without an embedded competitionGroup.
        shootout_scores (dict): Game id to (home, away) shootout result, for every finished shootout game.
    """

    club_id: str
    club_name: str
    season_id: str | None
    games: list[dict]
    club_names: dict[str, str]
    competition_names: dict[str, str]
    group_names: dict[str, str]
    shootout_scores: dict[str, tuple[int, int]]
    response: dict = field(default_factory=dict, init=False)
    URL_CLUB: ClassVar[str] = TMAPI_URL + "/club/{club_id}"
    # {season} is "?season=<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = TMAPI_URL + "/club/{club_id}/fixtures{season}"
    URL_ATTRIBUTES: ClassVar[str] = TMAPI_URL + "/attributes"

    @classmethod
    def fixtures_url(cls, club_id: str, season_id: str | None) -> str:
        season = f"?season={season_id}" if season_id else ""
        return cls.URL_TEMPLATE.format(club_id=club_id, season=season)

    @classmethod
    def parse_games(cls, content: bytes, *, club_id: str, season_id: str | None = None) -> list[dict]:
        """
        Unwrap and validate the fixtures response and return its games.

        Raises:
            UpstreamError: If the response does not have the expected shape, a game is not the club's or a finished
                game's score.gameResult is neither null nor "W", "D" or "L".
        """
        url = cls.fixtures_url(club_id, season_id)
        data = tmapi_data(url, content)
        games = data.get("games") if isinstance(data, dict) else None
        if not isinstance(games, list):
            raise UpstreamError(502, url, "Unexpected tmapi response: no games list")
        for game in games:
            validate_game(url, game)
            where = f"game {game['id']}"
            if club_id not in (game["homeClub"]["clubId"], game["awayClub"]["clubId"]):
                raise UpstreamError(502, url, f"Unexpected tmapi response: club {club_id} did not play {where}")
            result = game["score"].get("gameResult")
            if game["isFinished"] and result is not None and result not in GAME_RESULTS:
                raise UpstreamError(502, url, f"Unexpected tmapi response: score.gameResult={result!r} in {where}")
        return games

    @classmethod
    def from_bytes(
        cls,
        fixtures: bytes,
        *,
        club: bytes,
        clubs: Sequence[tuple[str, bytes]],
        competitions: Sequence[tuple[str, bytes]],
        attributes: bytes | None = None,
        games: Sequence[tuple[str, bytes]] = (),
        club_id: str,
        season_id: str | None = None,
    ) -> Self:
        """
        Build the service from already fetched responses: the fixtures and /club/{id} bodies, the (url, body) pairs of
        the clubs and competitions batch lookups and of the finished shootout games' /game/{id} reports and, if any
        game needs it, the /attributes body.
        """
        name, _ = parse_club(cls.URL_CLUB.format(club_id=club_id), club)
        group_names = {}
        if attributes is not None:
            group_names = competition_group_names(cls.URL_ATTRIBUTES, tmapi_data(cls.URL_ATTRIBUTES, attributes))
        rows = cls.parse_games(fixtures, club_id=club_id, season_id=season_id)
        reports = dict(games)
        return cls(
            club_id=club_id,
            club_name=name,
            season_id=season_id,
            games=rows,
            club_names={k: v for url, body in clubs for k, v in names_by_id(url, body).items()},
            competition_names={k: v for url, body in competitions for k, v in names_by_id(url, body).items()},
            group_names=group_names,
            shootout_scores={
                game["id"]: parse_shootout_score(url, reports[url], game=game)
                for game in rows
                if (url := URL_GAME.format(game_id=game["id"])) in reports
            },
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, club_id: str, season_id: str | None = None) -> Self:
        """
        Fetch the club (first, so an unknown club is a 404 before anything else is requested), its games in the season
        (the current one if not given), the names of their clubs and competitions, only if a game has no embedded
        competition group the /attributes group names, and the game report of every finished shootout game.
        """
        response = await client.get(cls.URL_CLUB.format(club_id=club_id))
        name, _ = parse_club(response.url, response.content)
        response = await client.get(cls.fixtures_url(club_id, season_id))
        games = cls.parse_games(response.content, club_id=club_id, season_id=season_id)
        club_ids = {game[side]["clubId"] for game in games for side in ("homeClub", "awayClub")}
        competition_ids = {game["baseDetails"]["competitionId"] for game in games}
        return cls(
            club_id=club_id,
            club_name=name,
            season_id=season_id,
            games=games,
            club_names=await lookup_names(client, "clubs", club_ids),
            competition_names=await lookup_names(client, "competitions", competition_ids),
            group_names=await fetch_group_names(client, games),
            shootout_scores=await fetch_shootout_scores(client, games),
        )

    def get_club_fixtures(self) -> dict:
        """
        Retrieve the club's games.

        Returns:
            dict: The club's id and name, the season (the requested one, else the seasonId shared by all games, else
                None) and its games, sorted by date and id.
        """
        games = []
        for game in self.games:
            row = parse_game(
                game,
                competition_names=self.competition_names,
                club_names=self.club_names,
                group_names=self.group_names,
                shootout_scores=self.shootout_scores,
            )
            row["venue"] = "home" if game["homeClub"]["clubId"] == self.club_id else "away"
            row["result"] = game["score"].get("gameResult") if game["isFinished"] else None
            games.append(row)
        season_ids = {game["seasonId"] for game in games}
        self.response["id"] = self.club_id
        self.response["name"] = self.club_name
        self.response["seasonId"] = self.season_id or (season_ids.pop() if len(season_ids) == 1 else None)
        self.response["games"] = sorted(games, key=lambda g: (g["date"], g["id"]))
        return self.response
