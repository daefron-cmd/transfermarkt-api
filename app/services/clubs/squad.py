from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.tmapi import TMAPI_URL, batch_urls, check_fields, get_attributes, parse_club, tmapi_data

SQUAD_ENTRY_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("playerId", str),
    ("shirtNumber", (int, type(None))),
    ("isCaptain", bool),
    ("type", str),
]
PLAYER_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("id", str),
    ("name", str),
    ("lifeDates.age", (int, type(None))),
    ("lifeDates.dateOfBirth", (str, type(None))),
    ("nationalityDetails.nationalities.nationalityId", int),
    ("nationalityDetails.nationalities.secondNationalityId", int),
    ("attributes.height", (int, float, type(None))),
    ("attributes.contractUntil", (str, type(None))),
]


@dataclass
class TransfermarktClubSquad:
    """
    A club's squad in a season, from tmapi's /club/{id}/squad (an unofficial JSON API; see app.tmapi), with each
    player's details from the /players batch lookup. Players are in tmapi's squad order and map as follows:

    - id: playerId; shirtNumber, isCaptain, type: the squad entry's ("current", "nationalTeam", "historical", ...)
    - name; age: lifeDates.age (the age at death for a deceased player); dateOfBirth: lifeDates.dateOfBirth
    - position: attributes.position.name; foot: attributes.preferredFoot.name (None when tmapi omits them)
    - nationalities: the /attributes country names of nationalityDetails.nationalities.nationalityId and
      secondNationalityId, each only when non-zero
    - height: attributes.height (metres) in centimetres, None when null or 0
    - contractUntil: attributes.contractUntil
    - marketValue: marketValueDetails.current.value, None when tmapi omits marketValueDetails or the value is 0 (tmapi's
      value for retired players)

    Args:
        club_id (str): The unique identifier of the club.
        club_name (str): The club's name.
        is_national_team (bool): Whether the club is a national team.
        season_id (str | None): The requested season, or None for the current squad.
        squad (list): The validated tmapi squad entries.
        players (dict): Player id to validated tmapi player.
        countries (dict): Country id to name.
    """

    club_id: str
    club_name: str
    is_national_team: bool
    season_id: str | None
    squad: list[dict]
    players: dict[str, dict]
    countries: dict[int, str]
    response: dict = field(default_factory=dict, init=False)
    URL_CLUB: ClassVar[str] = TMAPI_URL + "/club/{club_id}"
    # {season} is "?season=<id>", or empty for the current squad.
    URL_TEMPLATE: ClassVar[str] = TMAPI_URL + "/club/{club_id}/squad{season}"
    URL_ATTRIBUTES: ClassVar[str] = TMAPI_URL + "/attributes"
    URL_PLAYERS: ClassVar[str] = TMAPI_URL + "/players"

    @classmethod
    def squad_url(cls, club_id: str, season_id: str | None) -> str:
        season = f"?season={season_id}" if season_id else ""
        return cls.URL_TEMPLATE.format(club_id=club_id, season=season)

    @classmethod
    def parse_squad(cls, content: bytes, *, club_id: str, season_id: str | None = None) -> list[dict]:
        """
        Unwrap and validate the squad response and return its entries (possibly empty).

        Raises:
            UpstreamError: If the response does not have the expected shape.
        """
        url = cls.squad_url(club_id, season_id)
        data = tmapi_data(url, content)
        squad = data.get("squad") if isinstance(data, dict) else None
        if not isinstance(squad, list):
            raise UpstreamError(502, url, "Unexpected tmapi response: no squad list")
        for entry in squad:
            where = f"squad entry {entry.get('playerId') if isinstance(entry, dict) else None}"
            check_fields(url, entry, SQUAD_ENTRY_FIELDS, where)
        return squad

    @classmethod
    def parse_players(cls, responses: Sequence[tuple[str, bytes]], *, player_ids: set[str]) -> dict[str, dict]:
        """
        Unwrap and validate the (url, body) pairs of the /players batch lookups and return player id to player.

        Raises:
            UpstreamError: If a response does not have the expected shape or a player id is missing from the results.
        """
        players: dict[str, dict] = {}
        for url, content in responses:
            data = tmapi_data(url, content)
            if not isinstance(data, list):
                raise UpstreamError(502, url, "Unexpected tmapi response: players data is not a list")
            for player in data:
                where = f"player {player.get('id') if isinstance(player, dict) else None}"
                check_fields(url, player, PLAYER_FIELDS, where)
                for key in ("position", "preferredFoot"):
                    if key in player["attributes"]:
                        check_fields(url, player, [(f"attributes.{key}.name", str)], where)
                if "marketValueDetails" in player:
                    check_fields(url, player, [("marketValueDetails.current.value", int)], where)
                for path, value in [
                    ("lifeDates.dateOfBirth", player["lifeDates"]["dateOfBirth"]),
                    ("attributes.contractUntil", player["attributes"]["contractUntil"]),
                ]:
                    try:
                        if value is not None:
                            date.fromisoformat(value)
                    except ValueError as e:
                        raise UpstreamError(502, url, f"Unexpected tmapi response: {path}={value!r} in {where}") from e
                players[player["id"]] = player
        missing = sorted(player_ids - players.keys())
        if missing:
            raise UpstreamError(502, cls.URL_PLAYERS, f"tmapi lookup is missing ids {missing}")
        return players

    @classmethod
    def country_names(cls, attributes: Any) -> dict[int, str]:
        """Return country id to name from the /attributes data (its countries list)."""
        countries = attributes.get("countries") if isinstance(attributes, dict) else None
        if not isinstance(countries, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), int) and isinstance(c.get("name"), str) for c in countries
        ):
            raise UpstreamError(
                502, cls.URL_ATTRIBUTES, "Unexpected tmapi response: attributes.countries is not a list of {id, name}"
            )
        return {c["id"]: c["name"].strip() for c in countries}

    @classmethod
    def from_bytes(
        cls,
        squad: bytes,
        *,
        club: bytes,
        players: Sequence[tuple[str, bytes]],
        attributes: bytes | None = None,
        club_id: str,
        season_id: str | None = None,
    ) -> Self:
        """
        Build the service from already fetched responses: the squad and /club/{id} bodies, the (url, body) pairs of the
        /players batch lookups and, unless the squad is empty, the /attributes body.
        """
        name, is_national_team = parse_club(cls.URL_CLUB.format(club_id=club_id), club)
        entries = cls.parse_squad(squad, club_id=club_id, season_id=season_id)
        return cls(
            club_id=club_id,
            club_name=name,
            is_national_team=is_national_team,
            season_id=season_id,
            squad=entries,
            players=cls.parse_players(players, player_ids={entry["playerId"] for entry in entries}),
            countries=cls.country_names(tmapi_data(cls.URL_ATTRIBUTES, attributes)) if attributes is not None else {},
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, club_id: str, season_id: str | None = None) -> Self:
        """
        Fetch the club (first, so an unknown club is a 404 before anything else is requested), its squad in the season
        (the current squad if not given) and, unless the squad is empty, its players (50 per request) and the
        /attributes country names.
        """
        response = await client.get(cls.URL_CLUB.format(club_id=club_id))
        name, is_national_team = parse_club(response.url, response.content)
        response = await client.get(cls.squad_url(club_id, season_id))
        entries = cls.parse_squad(response.content, club_id=club_id, season_id=season_id)
        player_ids = {entry["playerId"] for entry in entries}
        responses = []
        for url in batch_urls("players", player_ids):
            response = await client.get(url)
            responses.append((response.url, response.content))
        return cls(
            club_id=club_id,
            club_name=name,
            is_national_team=is_national_team,
            season_id=season_id,
            squad=entries,
            players=cls.parse_players(responses, player_ids=player_ids),
            countries=cls.country_names(await get_attributes(client)) if entries else {},
        )

    def __parse_player(self, entry: dict) -> dict:
        player = self.players[entry["playerId"]]
        attributes = player["attributes"]
        nationalities = []
        for key in ("nationalityId", "secondNationalityId"):
            country_id = player["nationalityDetails"]["nationalities"][key]
            if not country_id:
                continue
            if country_id not in self.countries:
                raise UpstreamError(
                    502, self.URL_ATTRIBUTES, f"tmapi countries are missing id {country_id} of player {player['id']}"
                )
            nationalities.append(self.countries[country_id])
        height = attributes["height"]
        market_value = player["marketValueDetails"]["current"]["value"] if "marketValueDetails" in player else None
        return {
            "id": entry["playerId"],
            "name": player["name"].strip(),
            "shirtNumber": entry["shirtNumber"],
            "isCaptain": entry["isCaptain"],
            "position": attributes["position"]["name"] if "position" in attributes else None,
            "dateOfBirth": player["lifeDates"]["dateOfBirth"],
            "age": player["lifeDates"]["age"],
            "nationalities": nationalities,
            "height": round(height * 100) if height else None,
            "foot": attributes["preferredFoot"]["name"] if "preferredFoot" in attributes else None,
            "contractUntil": attributes["contractUntil"],
            "marketValue": market_value or None,
            "type": entry["type"],
        }

    def get_club_squad(self) -> dict:
        """
        Retrieve the club's squad.

        Returns:
            dict: The club's id, name, whether it is a national team, the requested season (None for the current
                squad) and its players in tmapi's squad order, as described on the class.
        """
        self.response["id"] = self.club_id
        self.response["name"] = self.club_name
        self.response["isNationalTeam"] = self.is_national_team
        self.response["seasonId"] = self.season_id
        self.response["players"] = [self.__parse_player(entry) for entry in self.squad]
        return self.response
