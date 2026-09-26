from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from app.http import TransfermarktClient, UpstreamError
from app.services.games import URL_GAME, competition_group_names, parse_game, parse_shootout_score, validate_game
from app.tmapi import TMAPI_URL, check_fields, get_attributes, lookup_names, tmapi_data

REPORT_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("score.home", (int, type(None))),
    ("baseDetails.competition.name", str),
    ("baseDetails.stadiumId", str),
    ("extendedDetails.duration", int),
    ("coaches.home.coachId", (str, type(None))),
    ("coaches.home.headCoachIds", list),
    ("coaches.away.coachId", (str, type(None))),
    ("coaches.away.headCoachIds", list),
    ("refereeIds.refereeId", (str, type(None))),
    ("playerIds", list),
    ("actions", list),
]
LINEUP_PLAYER_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("id", str),
    ("shirtNumber", (int, type(None))),
    ("isCaptain", bool),
    ("ageAtGameDate", (int, type(None))),
    ("marketValue.value", (int, type(None))),
]
ACTION_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("actionId", int),
    ("actionReasonId", int),
    ("minute", int),
    ("addedTime", int),
]
EVENT_TYPES = {
    "GOAL": "goal",
    "CARD": "card",
    "SUBSTITUTE": "substitution",
    "SHOOTOUT": "shootout",
    "MISSED_PENALTY": "missed_penalty",
    "COACH_SANCTION": "coach_sanction",
}
SIDES = {"home": "homeClub", "away": "awayClub"}


@dataclass
class TransfermarktGame:
    """
    A game's report, from tmapi's /game/{id} (an unofficial JSON API; see app.tmapi), with the names of its clubs,
    players, coaches and referee from the /clubs, /players, /coaches and /referees batch lookups and its stadium from
    /stadium/{id}. The report has no isLive or isFinished, so they are derived (the rule matched the fixture lists'
    flags on every game checked, but a live game was only seen in its second half): isLive is
    score.details.gameEndType == "Playing"; isFinished is a non-null score.home while not live. The game's fields are
    then mapped as described in app.services.games (competitionName: baseDetails.competition.name), plus:

    - stageLabel: baseDetails.tournamentStageLabel ("QF 3"), None when absent or ""
    - duration: extendedDetails.duration of a finished game (90, 120), None when 0 or the game has not finished
    - stadium: {id, name, city: location.city} from /stadium/{baseDetails.stadiumId}, None when stadiumId is "0" or
      tmapi has no stadium for the id (a 404)
    - referee: refereeIds.refereeId with its name, None when null
    - home/away: per club (an unplayed game has only clubId and headCoachIds, so its lists are empty and the rest None)
      - club: {id: clubId, name}; formation: tactic.tactic ("3-4-2-1")
      - coach: coaches.home/away.coachId, else its headCoachIds[0] (the current head coach before kick-off), else None
      - lineup / substitutes: lineup.players / lineup.substitutes: {id, name, shirtNumber, isCaptain, position:
        position.name (None when absent, as for substitutes with positionId 0), marketValue: marketValue.value, age:
        ageAtGameDate}
      - statistics: clubStatistics, tmapi's raw per-club statistics, passed through unchanged (None when absent)
    - events: the report's actions without PLACEHOLDER ones (kick-off, half time, ...), sorted by minute and addedTime
      (tmapi lists them latest first, so ties keep the reverse of its order): {type: EVENT_TYPES[type] (an unknown
      type lower-cased), minute, addedTime, club: {id: clubId, name} or None, action: the /attributes actions name of
      actionId, reason: the /attributes reasons name of actionReasonId (both None when not in the table; reason id 0
      is none), player: activePlayerId, relatedPlayer: passivePlayerId (both {id, name} or None), score: the running
      score {home, away} or None}. player is the scorer, carded player or shootout taker and relatedPlayer the
      assist (the fouled player for reason "Penalty: Fouled player"); in a substitution player is the player going
      off and relatedPlayer the one coming on (in the recorded finished reports every activePlayerId is in
      lineup.players and every passivePlayerId in lineup.substitutes). In a finished shootout game a shootout event's
      score is the shootout tally, like the shootout field (tmapi's running score includes the game's goals, so
      homeGoals/awayGoals are subtracted); a coach sanction has no player (tmapi gives only a coachId).

    Args:
        game_id (str): The unique identifier of the game.
        game (dict): The validated report data, with the derived isLive and isFinished.
        club_names, player_names, coach_names, referee_names (dict): Id to name.
        stadium (dict | None): The stadium's {id, name, city}, or None.
        action_names, reason_names (dict): /attributes action and reason id to name.
        group_names (dict): Competition group id to name, for a game without an embedded competitionGroup.
        shootout_scores (dict): The game id to its (home, away) shootout result, for a finished shootout game.
    """

    game_id: str
    game: dict
    club_names: dict[str, str]
    player_names: dict[str, str]
    coach_names: dict[str, str]
    referee_names: dict[str, str]
    stadium: dict | None
    action_names: dict[int, str]
    reason_names: dict[int, str]
    group_names: dict[str, str]
    shootout_scores: dict[str, tuple[int, int]]
    response: dict = field(default_factory=dict, init=False)
    URL_ATTRIBUTES: ClassVar[str] = TMAPI_URL + "/attributes"
    URL_STADIUM: ClassVar[str] = TMAPI_URL + "/stadium/{stadium_id}"

    @classmethod
    def parse_report(cls, content: bytes, *, game_id: str) -> dict:
        """
        Unwrap and validate the /game/{id} report and return its data with the derived isLive and isFinished.

        Raises:
            UpstreamError: 404 if tmapi reports success=false ("Game not found"), 502 if the report does not have the
                expected shape.
        """
        url = URL_GAME.format(game_id=game_id)
        where = f"game {game_id}"
        data = tmapi_data(url, content, failure_status=404)
        check_fields(url, data, REPORT_FIELDS, where)
        details = "details" in data["score"]
        if details:
            check_fields(url, data, [("score.details.gameEndType", str)], where)
        is_live = details and data["score"]["details"]["gameEndType"] == "Playing"
        game = {**data, "isLive": is_live, "isFinished": data["score"]["home"] is not None and not is_live}
        validate_game(url, game)
        if "tournamentStageLabel" in game["baseDetails"]:
            check_fields(url, game, [("baseDetails.tournamentStageLabel", str)], where)
        ids = game["playerIds"] + game["coaches"]["home"]["headCoachIds"] + game["coaches"]["away"]["headCoachIds"]
        if not all(isinstance(id_, str) for id_ in ids):
            raise UpstreamError(502, url, f"Unexpected tmapi response: playerIds or headCoachIds in {where}")
        for key in SIDES.values():
            club = game[key]
            if "lineup" in club:
                check_fields(url, club, [("lineup.players", list), ("lineup.substitutes", list)], f"{where} {key}")
                for player in club["lineup"]["players"] + club["lineup"]["substitutes"]:
                    player_where = f"{where} {key} player {player.get('id') if isinstance(player, dict) else None}"
                    check_fields(url, player, LINEUP_PLAYER_FIELDS, player_where)
                    if player.get("position") is not None:
                        check_fields(url, player, [("position.name", str)], player_where)
            if club.get("tactic") is not None:
                check_fields(url, club, [("tactic.tactic", str)], f"{where} {key}")
            if club.get("clubStatistics") is not None:
                check_fields(url, club, [("clubStatistics", dict)], f"{where} {key}")
        club_ids = (game["homeClub"]["clubId"], game["awayClub"]["clubId"])
        for action in game["actions"]:
            check_fields(url, action, [("type", str)], f"{where} action")
            if action["type"] == "PLACEHOLDER":
                continue
            action_where = f"{where} {action['type']} action"
            fields = ACTION_FIELDS.copy()
            fields += [(key, str) for key in ("clubId", "activePlayerId", "passivePlayerId") if key in action]
            fields += [("score.home", int), ("score.away", int)] if "score" in action else []
            check_fields(url, action, fields, action_where)
            if action.get("clubId", club_ids[0]) not in club_ids:
                raise UpstreamError(
                    502, url, f"Unexpected tmapi response: clubId={action['clubId']!r} in {action_where}"
                )
        return game

    @classmethod
    def parse_stadium(cls, content: bytes, *, stadium_id: str) -> dict:
        """
        Return the {id, name, city} of a stadium from its /stadium/{id} response.

        Raises:
            UpstreamError: If the response does not have the expected shape.
        """
        url = cls.URL_STADIUM.format(stadium_id=stadium_id)
        data = tmapi_data(url, content)
        check_fields(url, data, [("name", str), ("location.city", (str, type(None)))], f"stadium {stadium_id}")
        city = data["location"]["city"]
        return {"id": stadium_id, "name": data["name"].strip(), "city": city.strip() if city is not None else None}

    @classmethod
    def attribute_names(cls, attributes: Any, key: str, name: str) -> dict[int, str]:
        """Return id to name from an /attributes table such as actions ({id, action, type}) or reasons."""
        table = attributes.get(key) if isinstance(attributes, dict) else None
        if not isinstance(table, list) or not all(
            isinstance(row, dict) and isinstance(row.get("id"), int) and isinstance(row.get(name), str) for row in table
        ):
            raise UpstreamError(
                502, cls.URL_ATTRIBUTES, f"Unexpected tmapi response: attributes.{key} is not a list of {{id, {name}}}"
            )
        return {row["id"]: row[name] for row in table}

    @staticmethod
    def coach_id(game: dict, side: str) -> str | None:
        """The side's coachId, else the first of its headCoachIds, else None."""
        coaches = game["coaches"][side]
        return coaches["coachId"] or next(iter(coaches["headCoachIds"]), None)

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, game_id: str) -> Self:
        """
        Fetch the game report (first, so an unknown game is a 404 before anything else is requested), the /attributes
        tables, the names of its clubs, players (50 per request) and coaches, and only when set, its referee's name and
        its stadium (None when tmapi has no stadium for the id).
        """
        response = await client.get(URL_GAME.format(game_id=game_id))
        game = cls.parse_report(response.content, game_id=game_id)
        shootout_scores = {}
        if game["isFinished"] and game["score"]["additionType"] == "after_shootout":
            shootout_scores[game_id] = parse_shootout_score(response.url, response.content, game=game)
        attributes = await get_attributes(client)
        player_ids = set(game["playerIds"])
        for key in SIDES.values():
            lineup = game[key].get("lineup", {"players": [], "substitutes": []})
            player_ids |= {player["id"] for player in lineup["players"] + lineup["substitutes"]}
        for action in game["actions"]:
            player_ids |= {action[key] for key in ("activePlayerId", "passivePlayerId") if key in action}
        coach_ids = {coach_id for side in SIDES if (coach_id := cls.coach_id(game, side)) is not None}
        referee_id = game["refereeIds"]["refereeId"]
        stadium_id = game["baseDetails"]["stadiumId"]
        club_names = await lookup_names(client, "clubs", [game["homeClub"]["clubId"], game["awayClub"]["clubId"]])
        player_names = await lookup_names(client, "players", player_ids)
        coach_names = await lookup_names(client, "coaches", coach_ids)
        referee_names = await lookup_names(client, "referees", [referee_id]) if referee_id is not None else {}
        stadium = None
        if stadium_id != "0":
            try:
                response = await client.get(cls.URL_STADIUM.format(stadium_id=stadium_id))
            except UpstreamError as e:
                # tmapi answers an unknown stadium with 404 "Stadium not found".
                if e.status_code != 404:
                    raise
            else:
                stadium = cls.parse_stadium(response.content, stadium_id=stadium_id)
        return cls(
            game_id=game_id,
            game=game,
            club_names=club_names,
            player_names=player_names,
            coach_names=coach_names,
            referee_names=referee_names,
            stadium=stadium,
            action_names=cls.attribute_names(attributes, "actions", "action"),
            reason_names=cls.attribute_names(attributes, "reasons", "reason"),
            group_names=competition_group_names(cls.URL_ATTRIBUTES, attributes),
            shootout_scores=shootout_scores,
        )

    @staticmethod
    def __person(names: dict[str, str], id_: str | None) -> dict | None:
        return {"id": id_, "name": names[id_]} if id_ is not None else None

    def __parse_player(self, player: dict) -> dict:
        position = player.get("position")
        return {
            "id": player["id"],
            "name": self.player_names[player["id"]],
            "shirtNumber": player["shirtNumber"],
            "isCaptain": player["isCaptain"],
            "position": position["name"] if position is not None else None,
            "marketValue": player["marketValue"]["value"],
            "age": player["ageAtGameDate"],
        }

    def __parse_side(self, side: str) -> dict:
        club = self.game[SIDES[side]]
        lineup = club.get("lineup", {"players": [], "substitutes": []})
        tactic = club.get("tactic")
        return {
            "club": {"id": club["clubId"], "name": self.club_names[club["clubId"]]},
            "coach": self.__person(self.coach_names, self.coach_id(self.game, side)),
            "formation": tactic["tactic"] if tactic is not None else None,
            "lineup": [self.__parse_player(player) for player in lineup["players"]],
            "substitutes": [self.__parse_player(player) for player in lineup["substitutes"]],
            "statistics": club.get("clubStatistics"),
        }

    def __parse_event(self, action: dict) -> dict:
        club_id = action.get("clubId")
        score = action.get("score")
        # tmapi's shootout scores include the game's goals; the response's (set by get_game_report) are subtracted.
        if action["type"] == "SHOOTOUT" and score is not None and self.response["shootout"] is not None:
            score = {
                "home": score["home"] - self.response["homeGoals"],
                "away": score["away"] - self.response["awayGoals"],
            }
        return {
            "type": EVENT_TYPES.get(action["type"], action["type"].lower()),
            "minute": action["minute"],
            "addedTime": action["addedTime"],
            "club": self.__person(self.club_names, club_id),
            "action": self.action_names.get(action["actionId"]),
            "reason": self.reason_names.get(action["actionReasonId"]),
            "player": self.__person(self.player_names, action.get("activePlayerId")),
            "relatedPlayer": self.__person(self.player_names, action.get("passivePlayerId")),
            "score": score,
        }

    def get_game_report(self) -> dict:
        """
        Retrieve the game report.

        Returns:
            dict: The game's fields as in the fixtures endpoints plus stageLabel, duration, stadium, referee, home,
                away and events, as described on the class.
        """
        base = self.game["baseDetails"]
        self.response.update(
            parse_game(
                self.game,
                competition_names={base["competitionId"]: base["competition"]["name"].strip()},
                club_names=self.club_names,
                group_names=self.group_names,
                shootout_scores=self.shootout_scores,
            )
        )
        duration = self.game["extendedDetails"]["duration"]
        self.response["stageLabel"] = base.get("tournamentStageLabel") or None
        self.response["duration"] = duration if self.game["isFinished"] and duration else None
        self.response["stadium"] = self.stadium
        self.response["referee"] = self.__person(self.referee_names, self.game["refereeIds"]["refereeId"])
        self.response["home"] = self.__parse_side("home")
        self.response["away"] = self.__parse_side("away")
        actions = [action for action in reversed(self.game["actions"]) if action["type"] != "PLACEHOLDER"]
        self.response["events"] = [
            self.__parse_event(action) for action in sorted(actions, key=lambda a: (a["minute"], a["addedTime"]))
        ]
        return self.response
