"""
Games (matches) from tmapi fixture lists (an unofficial JSON API; see app.tmapi), shared by the fixtures endpoints
and the game report.

A tmapi game is mapped to the Game schema (app.schemas.games) as follows:

- id: id; seasonId: baseDetails.seasonId; seasonName: baseDetails.season.display ("24/25", or "2024" for
  calendar-year seasons); competitionId: baseDetails.competitionId; matchday: baseDetails.gameDay
- stage: the name of the competition group ("Group A", "Round of 16", "Qualifying Round 1st leg", ...) from the
  embedded baseDetails.competitionGroup or, when that is absent, from the /attributes competitionGroups table by
  baseDetails.competitionGroupId (the raw id if it is not in the table); None when competitionGroupId is "" (plain
  league rounds)
- date: baseDetails.date.dateTimeUTC (timezone-aware, UTC); isTimeDefined: baseDetails.date.isTimeDefined
- homeClub/awayClub: {id: homeClub.clubId / awayClub.clubId, name}
- homeGoals/awayGoals: score.home/score.away (None before kick-off), minus the shootout goals after a shootout
- endedAfter: score.additionType mapped by ENDED_AFTER (an unknown value is passed through unchanged); None when the
  game has not finished
- shootout: {home, away}, the penalty shootout result of a finished game with additionType "after_shootout", else
  None. tmapi's score then includes the shootout goals (1-1 and 5-3 on penalties is 6-4), so the score after regular
  or extra time is read from the game report (/game/{id}): the running score of its last GOAL action (by minute and
  addedTime, then by total goals), 0-0 without GOAL actions; the shootout is tmapi's score minus that
- isFinished, isLive; attendance: extendedDetails.crowdSize; url: relativeUrl
"""

from datetime import UTC, datetime
from typing import Any

from app.http import TransfermarktClient, UpstreamError
from app.tmapi import TMAPI_URL, check_fields, get_attributes, tmapi_data

URL_GAME = TMAPI_URL + "/game/{game_id}"
ENDED_AFTER = {"none": "regular", "after_extra_time": "extra_time", "after_shootout": "shootout"}

GAME_FIELDS: list[tuple[str, type | tuple[type, ...]]] = [
    ("id", str),
    ("baseDetails.seasonId", int),
    ("baseDetails.gameDay", int),
    ("baseDetails.competitionId", str),
    ("baseDetails.competitionGroupId", str),
    ("baseDetails.season.display", str),
    ("baseDetails.date.dateTimeUTC", str),
    ("baseDetails.date.isTimeDefined", bool),
    ("extendedDetails.crowdSize", (int, type(None))),
    ("homeClub.clubId", str),
    ("awayClub.clubId", str),
    ("score.home", (int, type(None))),
    ("score.away", (int, type(None))),
    ("score.additionType", str),
    ("isLive", bool),
    ("isFinished", bool),
    ("relativeUrl", str),
]


def validate_game(url: str, game: Any) -> None:
    """
    Check the fields parse_game relies on; a changed upstream shape must fail loudly, not return wrong games.

    Raises:
        UpstreamError: 502 naming the game id and the offending key.
    """
    where = f"game {game.get('id') if isinstance(game, dict) else None}"
    check_fields(url, game, GAME_FIELDS, where)
    if "competitionGroup" in game["baseDetails"]:
        check_fields(url, game, [("baseDetails.competitionGroup.name", str)], where)
    date = game["baseDetails"]["date"]["dateTimeUTC"]
    try:
        parsed = datetime.fromisoformat(date)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise UpstreamError(502, url, f"Unexpected tmapi response: baseDetails.date.dateTimeUTC={date!r} in {where}")


def competition_group_names(url: str, attributes: Any) -> dict[str, str]:
    """Return competition group id to name from the /attributes data (its competitionGroups list)."""
    groups = attributes.get("competitionGroups") if isinstance(attributes, dict) else None
    if not isinstance(groups, list) or not all(
        isinstance(g, dict) and isinstance(g.get("id"), str) and isinstance(g.get("name"), str) for g in groups
    ):
        raise UpstreamError(
            502, url, "Unexpected tmapi response: attributes.competitionGroups is not a list of {id, name, ...}"
        )
    return {g["id"]: g["name"] for g in groups}


async def fetch_group_names(client: TransfermarktClient, games: list[dict]) -> dict[str, str]:
    """
    Return the competition group names needed by validated `games`: fetched from /attributes only if a game has a
    non-empty competitionGroupId without an embedded competitionGroup, otherwise empty.
    """
    base_details = [game["baseDetails"] for game in games]
    if not any(base["competitionGroupId"] and "competitionGroup" not in base for base in base_details):
        return {}
    return competition_group_names(f"{TMAPI_URL}/attributes", await get_attributes(client))


def parse_shootout_score(url: str, content: bytes, *, game: dict) -> tuple[int, int]:
    """
    Return the (home, away) penalty shootout result of a validated shootout `game` from its /game/{id} report, as
    described in the module docstring.

    Raises:
        UpstreamError: 502 if the report does not have the expected shape or the result is not a plausible shootout.
    """
    where = f"game {game['id']}"
    check_fields(url, game, [("score.home", int), ("score.away", int)], where)
    data = tmapi_data(url, content)
    check_fields(url, data, [("actions", list)], where)
    goals = []
    for action in data["actions"]:
        check_fields(url, action, [("type", str)], f"{where} action")
        if action["type"] == "GOAL":
            fields: list[tuple[str, type | tuple[type, ...]]] = [
                ("minute", int),
                ("addedTime", int),
                ("score.home", int),
                ("score.away", int),
            ]
            check_fields(url, action, fields, f"{where} GOAL action")
            goals.append(action)
    last = max(
        goals,
        key=lambda a: (a["minute"], a["addedTime"], a["score"]["home"] + a["score"]["away"]),
        default={"score": {"home": 0, "away": 0}},
    )
    home = game["score"]["home"] - last["score"]["home"]
    away = game["score"]["away"] - last["score"]["away"]
    if home < 0 or away < 0 or home + away < 1:
        raise UpstreamError(502, url, f"Unexpected tmapi response: shootout score {home}-{away} in {where}")
    return home, away


async def fetch_shootout_scores(client: TransfermarktClient, games: list[dict]) -> dict[str, tuple[int, int]]:
    """Fetch the game reports of the finished shootout games among validated `games`; return their shootout results."""
    scores: dict[str, tuple[int, int]] = {}
    for game in games:
        if game["isFinished"] and game["score"]["additionType"] == "after_shootout":
            response = await client.get(URL_GAME.format(game_id=game["id"]))
            scores[game["id"]] = parse_shootout_score(response.url, response.content, game=game)
    return scores


def parse_game(
    game: dict,
    *,
    competition_names: dict[str, str],
    club_names: dict[str, str],
    group_names: dict[str, str],
    shootout_scores: dict[str, tuple[int, int]],
) -> dict:
    """Map a validated tmapi game to the Game schema's fields, as described in the module docstring."""
    base = game["baseDetails"]
    group_id = base["competitionGroupId"]
    if not group_id:
        stage = None
    elif "competitionGroup" in base:
        stage = base["competitionGroup"]["name"]
    else:
        stage = group_names.get(group_id, group_id)
    home_id, away_id = game["homeClub"]["clubId"], game["awayClub"]["clubId"]
    addition_type = game["score"]["additionType"]
    ended_after = ENDED_AFTER.get(addition_type, addition_type) if game["isFinished"] else None
    home_goals, away_goals = game["score"]["home"], game["score"]["away"]
    shootout = None
    if ended_after == "shootout":
        shootout_home, shootout_away = shootout_scores[game["id"]]
        shootout = {"home": shootout_home, "away": shootout_away}
        home_goals, away_goals = home_goals - shootout_home, away_goals - shootout_away
    return {
        "id": game["id"],
        "seasonId": str(base["seasonId"]),
        "seasonName": base["season"]["display"],
        "competitionId": base["competitionId"],
        "competitionName": competition_names[base["competitionId"]],
        "matchday": base["gameDay"],
        "stage": stage,
        "date": datetime.fromisoformat(base["date"]["dateTimeUTC"]).astimezone(UTC),
        "isTimeDefined": base["date"]["isTimeDefined"],
        "homeClub": {"id": home_id, "name": club_names[home_id]},
        "awayClub": {"id": away_id, "name": club_names[away_id]},
        "homeGoals": home_goals,
        "awayGoals": away_goals,
        "endedAfter": ended_after,
        "shootout": shootout,
        "isFinished": game["isFinished"],
        "isLive": game["isLive"],
        "attendance": game["extendedDetails"]["crowdSize"],
        "url": game["relativeUrl"],
    }
