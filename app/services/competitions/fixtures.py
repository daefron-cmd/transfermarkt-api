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
from app.tmapi import TMAPI_URL, lookup_names, names_by_id, parse_competition, tmapi_data


@dataclass
class TransfermarktCompetitionFixtures:
    """
    A competition's games in a season, from tmapi's /competition/{id}/fixtures (an unofficial JSON API; see
    app.tmapi). The per-matchday rounds are flattened into one list of games (mapped as described in
    app.services.games), sorted by matchday, date and id, and optionally filtered to one matchday.

    Args:
        competition_id (str): The unique identifier of the competition.
        competition_name (str): The competition's name.
        season_id (str): The season of the games (the requested one, or the competition's current season).
        games (list): The validated tmapi games.
        club_names (dict): Club id to name.
        group_names (dict): Competition group id to name, for games without an embedded competitionGroup.
        shootout_scores (dict): Game id to (home, away) shootout result, for every finished shootout game.
    """

    competition_id: str
    competition_name: str
    season_id: str
    games: list[dict]
    club_names: dict[str, str]
    group_names: dict[str, str]
    shootout_scores: dict[str, tuple[int, int]]
    response: dict = field(default_factory=dict, init=False)
    URL_COMPETITION: ClassVar[str] = TMAPI_URL + "/competition/{competition_id}"
    # {season} is "?season=<id>", or empty for the current season.
    URL_TEMPLATE: ClassVar[str] = TMAPI_URL + "/competition/{competition_id}/fixtures{season}"
    URL_ATTRIBUTES: ClassVar[str] = TMAPI_URL + "/attributes"

    @classmethod
    def fixtures_url(cls, competition_id: str, season_id: str | None) -> str:
        season = f"?season={season_id}" if season_id else ""
        return cls.URL_TEMPLATE.format(competition_id=competition_id, season=season)

    @classmethod
    def parse_games(
        cls, content: bytes, *, competition_id: str, season_id: str | None = None, matchday: int | None = None
    ) -> list[dict]:
        """
        Unwrap and validate the fixtures response and return its games (every round's), filtered to `matchday` if
        given.

        Raises:
            UpstreamError: If the response does not have the expected shape.
        """
        url = cls.fixtures_url(competition_id, season_id)
        data = tmapi_data(url, content)
        rounds = data.get("fixtures") if isinstance(data, dict) else None
        if not isinstance(rounds, list):
            raise UpstreamError(502, url, "Unexpected tmapi response: no fixtures list")
        games = []
        for i, round_ in enumerate(rounds):
            if not isinstance(round_, dict) or not isinstance(round_.get("games"), list):
                raise UpstreamError(502, url, f"Unexpected tmapi response: no games list in fixtures[{i}]")
            games += round_["games"]
        for game in games:
            validate_game(url, game)
        if matchday is not None:
            games = [game for game in games if game["baseDetails"]["gameDay"] == matchday]
        return games

    @staticmethod
    def club_ids(games: list[dict]) -> set[str]:
        return {game[side]["clubId"] for game in games for side in ("homeClub", "awayClub")}

    @classmethod
    def from_bytes(
        cls,
        fixtures: bytes,
        *,
        competition: bytes,
        clubs: Sequence[tuple[str, bytes]],
        attributes: bytes | None = None,
        games: Sequence[tuple[str, bytes]] = (),
        competition_id: str,
        season_id: str | None = None,
        matchday: int | None = None,
    ) -> Self:
        """
        Build the service from already fetched responses: the fixtures and /competition/{id} bodies, the (url, body)
        pairs of the clubs batch lookups and of the finished shootout games' /game/{id} reports and, if any game needs
        it, the /attributes body.
        """
        name, current_season_id = parse_competition(
            cls.URL_COMPETITION.format(competition_id=competition_id), competition
        )
        group_names = {}
        if attributes is not None:
            group_names = competition_group_names(cls.URL_ATTRIBUTES, tmapi_data(cls.URL_ATTRIBUTES, attributes))
        rows = cls.parse_games(fixtures, competition_id=competition_id, season_id=season_id, matchday=matchday)
        reports = dict(games)
        return cls(
            competition_id=competition_id,
            competition_name=name,
            season_id=season_id or str(current_season_id),
            games=rows,
            club_names={k: v for url, body in clubs for k, v in names_by_id(url, body).items()},
            group_names=group_names,
            shootout_scores={
                game["id"]: parse_shootout_score(url, reports[url], game=game)
                for game in rows
                if (url := URL_GAME.format(game_id=game["id"])) in reports
            },
        )

    @classmethod
    async def fetch(
        cls,
        client: TransfermarktClient,
        *,
        competition_id: str,
        season_id: str | None = None,
        matchday: int | None = None,
    ) -> Self:
        """
        Fetch the competition (first, so an unknown competition is a 404 before anything else is requested), its games
        in the season (the current one if not given), the names of their clubs, only if a game has no embedded
        competition group the /attributes group names, and the game report of every finished shootout game.
        """
        response = await client.get(cls.URL_COMPETITION.format(competition_id=competition_id))
        name, current_season_id = parse_competition(response.url, response.content)
        response = await client.get(cls.fixtures_url(competition_id, season_id))
        games = cls.parse_games(response.content, competition_id=competition_id, season_id=season_id, matchday=matchday)
        return cls(
            competition_id=competition_id,
            competition_name=name,
            season_id=season_id or str(current_season_id),
            games=games,
            club_names=await lookup_names(client, "clubs", cls.club_ids(games)),
            group_names=await fetch_group_names(client, games),
            shootout_scores=await fetch_shootout_scores(client, games),
        )

    def get_competition_fixtures(self) -> dict:
        """
        Retrieve the competition's games.

        Returns:
            dict: The competition's id, name and season and its games, sorted by matchday, date and id.
        """
        games = [
            parse_game(
                game,
                competition_names={self.competition_id: self.competition_name},
                club_names=self.club_names,
                group_names=self.group_names,
                shootout_scores=self.shootout_scores,
            )
            for game in self.games
        ]
        self.response["id"] = self.competition_id
        self.response["name"] = self.competition_name
        self.response["seasonId"] = self.season_id
        self.response["games"] = sorted(games, key=lambda g: (g["matchday"], g["date"], g["id"]))
        return self.response
