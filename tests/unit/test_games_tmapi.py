"""Parser tests for the tmapi-backed game report service, on recorded fixtures."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable

import pytest

from app.http import UpstreamError
from app.schemas.games import GameReport
from app.services.games import URL_GAME
from app.services.games_report import TransfermarktGame
from app.tmapi import TMAPI_URL, batch_urls
from tests.unit.tmapi_helpers import DoctoredClient, RecordingClient, envelope, fixture_bytes


def report_data(game_id: str) -> dict:
    return json.loads(fixture_bytes(URL_GAME.format(game_id=game_id)))["data"]


def game_report(
    game_id: str,
    doctor: Callable[[dict], None] | None = None,
    overrides: dict[str, bytes | UpstreamError] | None = None,
) -> tuple[dict, RecordingClient]:
    """The game report response (with `doctor` applied to the recorded report data) and the client that fetched it."""
    overrides = dict(overrides or {})
    if doctor is not None:
        data = report_data(game_id)
        doctor(data)
        overrides[URL_GAME.format(game_id=game_id)] = envelope(data)
    client = DoctoredClient(overrides)
    tfmkt = asyncio.run(TransfermarktGame.fetch(client, game_id=game_id))  # type: ignore[arg-type]
    return GameReport.model_validate(tfmkt.get_game_report()).model_dump(by_alias=True, mode="json"), client


def action_at(data: dict, type_: str, minute: int) -> dict:
    return next(a for a in data["actions"] if a["type"] == type_ and a["minute"] == minute)


def test_report_shootout_game():
    response, _ = game_report("4359338")
    assert (response["homeClub"]["name"], response["awayClub"]["name"]) == ("England", "Switzerland")
    assert (response["homeGoals"], response["awayGoals"], response["shootout"]) == (1, 1, {"home": 5, "away": 3})
    assert (response["endedAfter"], response["isFinished"], response["isLive"]) == ("shootout", True, False)
    assert (response["competitionName"], response["stage"], response["stageLabel"]) == (
        "UEFA Euro",
        "Quarter-Finals",
        "QF 3",
    )
    assert response["duration"] == 120
    assert response["stadium"] == {"id": "35", "name": "MERKUR SPIEL-ARENA", "city": "Düsseldorf"}
    assert response["referee"] == {"id": "906", "name": "Daniele Orsato"}
    home = response["home"]
    assert home["club"] == {"id": "3299", "name": "England"}
    assert home["coach"] == {"id": "3358", "name": "Gareth Southgate"}
    assert home["formation"] == "3-4-2-1"
    assert (len(home["lineup"]), len(home["substitutes"])) == (11, 14)
    assert home["lineup"][0] == {
        "id": "130164",
        "name": "Jordan Pickford",
        "shirtNumber": 1,
        "isCaptain": False,
        "position": "Goalkeeper",
        "marketValue": 22000000,
        "age": 30,
    }
    assert home["statistics"] == report_data("4359338")["homeClub"]["clubStatistics"]


def test_report_events_are_chronological_without_placeholders():
    data = report_data("4359338")
    events = game_report("4359338")[0]["events"]
    assert len(events) == len([a for a in data["actions"] if a["type"] != "PLACEHOLDER"]) == 25
    assert [(e["minute"], e["addedTime"]) for e in events] == sorted((e["minute"], e["addedTime"]) for e in events)
    assert Counter(e["type"] for e in events) == {"substitution": 11, "shootout": 9, "card": 3, "goal": 2}
    goal = next(e for e in events if e["type"] == "goal" and e["minute"] == 80)
    assert goal == {
        "type": "goal",
        "minute": 80,
        "addedTime": 0,
        "club": {"id": "3299", "name": "England"},
        "action": "Left-footed shot",
        "reason": "Pass",
        "player": {"id": "433177", "name": "Bukayo Saka"},
        "relatedPlayer": {"id": "357662", "name": "Declan Rice"},
        "score": {"home": 1, "away": 1},
    }
    # All shootout kicks share minute 120+30; tmapi lists them latest first, so the reverse is the kick order.
    shootout = [(e["player"]["name"], e["action"], e["score"]) for e in events if e["type"] == "shootout"]
    assert shootout[:2] == [
        ("Cole Palmer", "Scored", {"home": 1, "away": 0}),
        ("Manuel Akanji", "Saved", None),
    ]
    assert shootout[-1] == ("Trent Alexander-Arnold", "Scored", {"home": 5, "away": 3})


def test_report_shootout_event_scores_are_the_shootout_tally():
    # tmapi's shootout scores include the 1-1 of the game: the first scored kick is 2-1 and the last 6-4.
    raw = [a["score"] for a in report_data("4359338")["actions"] if a["type"] == "SHOOTOUT" and "score" in a]
    assert (raw[-1], raw[0]) == ({"home": 2, "away": 1}, {"home": 6, "away": 4})
    response, _ = game_report("4359338")
    scores = [e["score"] for e in response["events"] if e["type"] == "shootout" and e["score"] is not None]
    assert (scores[0], scores[-1]) == ({"home": 1, "away": 0}, {"home": 5, "away": 3})
    assert scores[-1] == response["shootout"]
    # Other events keep the running score of the game.
    assert [e["score"] for e in response["events"] if e["type"] == "goal"] == [
        {"home": 0, "away": 1},
        {"home": 1, "away": 1},
    ]


def test_report_event_types():
    def doctor(data: dict) -> None:
        action_at(data, "CARD", 37)["type"] = "VAR_DECISION"

    events = game_report("4588078", doctor)[0]["events"]
    types = Counter(e["type"] for e in events)
    assert types == {"card": 9, "substitution": 11, "goal": 5, "coach_sanction": 1, "var_decision": 1}
    sanction = next(e for e in events if e["type"] == "coach_sanction")
    assert (sanction["club"]["name"], sanction["action"], sanction["player"]) == ("Real Madrid", "Yellow card", None)


def test_report_unknown_action_and_reason_are_none():
    def doctor(data: dict) -> None:
        action_at(data, "GOAL", 80).update(actionId=999, actionReasonId=999)

    events = game_report("4359338", doctor)[0]["events"]
    goal = next(e for e in events if e["type"] == "goal" and e["minute"] == 80)
    assert (goal["action"], goal["reason"]) == (None, None)
    # Shootout kicks have actionReasonId 0.
    assert {e["reason"] for e in events if e["type"] == "shootout"} == {None}


def test_report_substitution_roles():
    response, _ = game_report("4359338")
    for side in ("home", "away"):
        lineup = {p["id"] for p in response[side]["lineup"]}
        substitutes = {p["id"] for p in response[side]["substitutes"]}
        subs = [e for e in response["events"] if e["type"] == "substitution" and e["club"] == response[side]["club"]]
        assert subs
        assert all(e["player"]["id"] in lineup and e["relatedPlayer"]["id"] in substitutes for e in subs)
    kane = next(
        e
        for e in response["events"]
        if e["type"] == "substitution" and e["minute"] == 109 and e["addedTime"] == 0 and e["club"]["id"] == "3299"
    )
    assert (kane["player"]["name"], kane["relatedPlayer"]["name"], kane["reason"]) == (
        "Harry Kane",
        "Ivan Toney",
        "Injury",
    )


def test_report_coach_falls_back_to_head_coach():
    def doctor(data: dict) -> None:
        data["coaches"]["home"]["coachId"] = None

    response, client = game_report("4359338", doctor)
    assert response["home"]["coach"] == {"id": "3358", "name": "Gareth Southgate"}
    assert f"{TMAPI_URL}/coaches?ids[]=3240&ids[]=3358" in client.urls


def test_report_coach_is_none_without_head_coach():
    def doctor(data: dict) -> None:
        data["coaches"]["home"]["headCoachIds"] = []

    coaches_url = f"{TMAPI_URL}/coaches?ids[]=67"
    response, client = game_report("4918697", doctor, {coaches_url: envelope([{"id": "67", "name": "Hansi Flick"}])})
    assert (response["home"]["coach"], response["away"]["coach"]) == (None, {"id": "67", "name": "Hansi Flick"})
    assert coaches_url in client.urls


def test_report_unplayed_game():
    response, client = game_report("4918697")
    assert (response["homeGoals"], response["awayGoals"], response["endedAfter"], response["shootout"]) == (
        None,
        None,
        None,
        None,
    )
    assert (response["isFinished"], response["isLive"]) == (False, False)
    assert {k: response[k] for k in ("duration", "stadium", "referee", "stageLabel", "attendance")} == dict.fromkeys(
        ("duration", "stadium", "referee", "stageLabel", "attendance")
    )
    assert response["events"] == []
    for side in ("home", "away"):
        assert {k: response[side][k] for k in ("formation", "lineup", "substitutes", "statistics")} == {
            "formation": None,
            "lineup": [],
            "substitutes": [],
            "statistics": None,
        }
    # coachId is null before kick-off, so the coach is the club's head coach.
    assert (response["home"]["coach"]["name"], response["away"]["coach"]["name"]) == ("José Bordalás", "Hansi Flick")
    assert client.urls == [
        URL_GAME.format(game_id="4918697"),
        f"{TMAPI_URL}/attributes",
        f"{TMAPI_URL}/clubs?ids[]=131&ids[]=3709",
        f"{TMAPI_URL}/coaches?ids[]=6440&ids[]=67",
    ]


def test_report_live_game_is_derived_from_game_end_type():
    def doctor(data: dict) -> None:
        data["score"]["details"]["gameEndType"] = "Playing"

    response, _ = game_report("4909471", doctor)
    assert (response["isLive"], response["isFinished"], response["endedAfter"], response["duration"]) == (
        True,
        False,
        None,
        None,
    )
    assert (response["homeGoals"], response["awayGoals"]) == (0, 5)


def test_report_stadium_zero_is_none_without_request():
    def doctor(data: dict) -> None:
        data["baseDetails"]["stadiumId"] = "0"

    response, client = game_report("4909471", doctor)
    assert response["stadium"] is None
    assert not any("/stadium/" in url for url in client.urls)


def test_report_unknown_stadium_is_none():
    url = f"{TMAPI_URL}/stadium/1272"
    response, client = game_report("4909471", overrides={url: UpstreamError(404, url, "Client Error. Not Found")})
    assert response["stadium"] is None
    assert client.urls[-1] == url
    assert response["referee"] is not None


def test_report_stadium_error_other_than_404_is_raised():
    url = f"{TMAPI_URL}/stadium/1272"
    with pytest.raises(UpstreamError) as e:
        game_report("4909471", overrides={url: UpstreamError(502, url, "Timeout")})
    assert e.value.status_code == 502


def test_report_requests():
    _, client = game_report("4359338")
    data = report_data("4359338")
    assert client.urls == [
        URL_GAME.format(game_id="4359338"),
        f"{TMAPI_URL}/attributes",
        f"{TMAPI_URL}/clubs?ids[]=3299&ids[]=3384",
        *batch_urls("players", data["playerIds"]),
        f"{TMAPI_URL}/coaches?ids[]=3240&ids[]=3358",
        f"{TMAPI_URL}/referees?ids[]=906",
        f"{TMAPI_URL}/stadium/35",
    ]
    assert len(batch_urls("players", data["playerIds"])) == 2


def test_report_unknown_game_is_404():
    client = RecordingClient()
    with pytest.raises(UpstreamError) as e:
        asyncio.run(TransfermarktGame.fetch(client, game_id="0"))  # type: ignore[arg-type]
    assert e.value.status_code == 404
    assert client.urls == [URL_GAME.format(game_id="0")]


def test_report_game_not_found_envelope_is_404():
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.parse_report(b'{"success":false,"message":"Game not found"}', game_id="0")
    assert e.value.status_code == 404
    assert "Game not found" in e.value.reason


@pytest.mark.parametrize(
    ("doctor", "message"),
    [
        (lambda d: d["homeClub"]["lineup"].update(players={}), "lineup.players={} in game 4359338 homeClub"),
        (lambda d: action_at(d, "GOAL", 80).update(minute="80"), "minute='80' in game 4359338 GOAL action"),
        (lambda d: action_at(d, "GOAL", 80).update(clubId="1"), "clubId='1' in game 4359338 GOAL action"),
        (lambda d: d["homeClub"]["lineup"]["players"][0].update(isCaptain=None), "isCaptain=None"),
        (lambda d: d["score"]["details"].pop("gameEndType"), "no score.details.gameEndType in game 4359338"),
        (lambda d: d["baseDetails"]["competition"].pop("name"), "no baseDetails.competition.name"),
        (lambda d: d.update(playerIds=[1]), "playerIds or headCoachIds in game 4359338"),
    ],
)
def test_report_unexpected_shape_is_502(doctor, message):
    data = report_data("4359338")
    doctor(data)
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.parse_report(envelope(data), game_id="4359338")
    assert e.value.status_code == 502
    assert message in e.value.reason


def test_report_player_missing_from_lookup_is_502():
    def doctor(data: dict) -> None:
        action_at(data, "GOAL", 80)["passivePlayerId"] = "999999999"

    # The recorded lookup of the batch holding the unknown id, which does not have it.
    url = f"{TMAPI_URL}/players?ids[]=95810&ids[]=999999999"
    with pytest.raises(UpstreamError) as e:
        game_report("4359338", doctor, {url: fixture_bytes(f"{TMAPI_URL}/players?ids[]=95810")})
    assert e.value.status_code == 502
    assert "missing ids ['999999999']" in e.value.reason


def raise_report_error(doctor: Callable[[dict], None], game_id: str = "4359338") -> UpstreamError:
    data = report_data(game_id)
    doctor(data)
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.parse_report(envelope(data), game_id=game_id)
    return e.value


def test_report_body_not_json_is_502_for_the_game_url():
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.parse_report(b"<html>", game_id="4359338")
    assert (e.value.status_code, e.value.url, e.value.reason) == (
        502,
        URL_GAME.format(game_id="4359338"),
        "Unexpected tmapi response: body is not JSON",
    )


def first_home_player(data: dict) -> dict:
    return data["homeClub"]["lineup"]["players"][0]


def goal_at_80(data: dict) -> dict:
    return action_at(data, "GOAL", 80)


@pytest.mark.parametrize(
    ("doctor", "reason"),
    [
        (lambda d: d["baseDetails"]["competition"].pop("name"), "no baseDetails.competition.name in game 4359338"),
        (lambda d: d["score"]["details"].update(gameEndType=1), "score.details.gameEndType=1 in game 4359338"),
        (
            lambda d: d["baseDetails"]["date"].update(dateTimeUTC="soon"),
            "baseDetails.date.dateTimeUTC='soon' in game 4359338",
        ),
        (
            lambda d: d["baseDetails"].update(tournamentStageLabel=3),
            "baseDetails.tournamentStageLabel=3 in game 4359338",
        ),
        (lambda d: d.update(playerIds=["1", 2]), "playerIds or headCoachIds in game 4359338"),
        (lambda d: d["awayClub"]["lineup"].update(substitutes={}), "lineup.substitutes={} in game 4359338 awayClub"),
        (
            lambda d: first_home_player(d).update(isCaptain=None),
            "isCaptain=None in game 4359338 homeClub player 130164",
        ),
        (
            lambda d: d["homeClub"]["lineup"]["players"].insert(0, "Pickford"),
            "no id in game 4359338 homeClub player None",
        ),
        (
            lambda d: first_home_player(d).update(position={}),
            "no position.name in game 4359338 homeClub player 130164",
        ),
        (lambda d: d["homeClub"].update(tactic={}), "no tactic.tactic in game 4359338 homeClub"),
        (lambda d: d["awayClub"].update(clubStatistics=[]), "clubStatistics=[] in game 4359338 awayClub"),
        (lambda d: d["actions"].append({}), "no type in game 4359338 action"),
        (lambda d: goal_at_80(d)["score"].update(home="1"), "score.home='1' in game 4359338 GOAL action"),
        (lambda d: goal_at_80(d).update(activePlayerId=1), "activePlayerId=1 in game 4359338 GOAL action"),
        (lambda d: goal_at_80(d).update(passivePlayerId=2), "passivePlayerId=2 in game 4359338 GOAL action"),
        # The type check names clubId before the player ids; the membership check alone would name activePlayerId.
        (lambda d: goal_at_80(d).update(clubId=1, activePlayerId=2), "clubId=1 in game 4359338 GOAL action"),
        (lambda d: goal_at_80(d).update(clubId="1"), "clubId='1' in game 4359338 GOAL action"),
    ],
)
def test_report_unexpected_shape_detail(doctor, reason):
    error = raise_report_error(doctor)
    assert (error.status_code, error.url, error.reason) == (
        502,
        URL_GAME.format(game_id="4359338"),
        f"Unexpected tmapi response: {reason}",
    )


def test_report_placeholder_actions_are_not_checked():
    def doctor(data: dict) -> None:
        data["actions"] = [{"type": "PLACEHOLDER"} if a["type"] == "PLACEHOLDER" else a for a in data["actions"]]

    data = report_data("4359338")
    doctor(data)
    game = TransfermarktGame.parse_report(envelope(data), game_id="4359338")
    assert [a for a in game["actions"] if a["type"] == "PLACEHOLDER"] == [{"type": "PLACEHOLDER"}] * 9
    assert len(game_report("4359338", doctor)[0]["events"]) == 25


def test_report_action_without_club_has_no_club():
    def doctor(data: dict) -> None:
        del goal_at_80(data)["clubId"]

    events = game_report("4359338", doctor)[0]["events"]
    goals = [(e["minute"], e["club"]) for e in events if e["type"] == "goal"]
    assert goals == [(75, {"id": "3384", "name": "Switzerland"}), (80, None)]


def test_report_fetch_error_names_the_game():
    with pytest.raises(UpstreamError) as e:
        game_report("4359338", lambda d: d.update(playerIds=[1]))
    assert (e.value.url, e.value.reason) == (
        URL_GAME.format(game_id="4359338"),
        "Unexpected tmapi response: playerIds or headCoachIds in game 4359338",
    )


def test_report_implausible_shootout_is_502_for_the_game_url():
    def doctor(data: dict) -> None:
        data["score"].update(home=1, away=1)

    with pytest.raises(UpstreamError) as e:
        game_report("4359338", doctor)
    assert (e.value.status_code, e.value.url, e.value.reason) == (
        502,
        URL_GAME.format(game_id="4359338"),
        "Unexpected tmapi response: shootout score 0-0 in game 4359338",
    )


def test_report_player_lookup_covers_lineups_and_actions():
    # With no playerIds, the looked-up ids are the lineups', the substitutes' and the actions' players.
    data = report_data("4909471")
    goal = next(a for a in data["actions"] if a["type"] == "GOAL")
    ids = {"999"}
    for key in ("homeClub", "awayClub"):
        ids |= {p["id"] for p in data[key]["lineup"]["players"] + data[key]["lineup"]["substitutes"]}
    ids |= {a[k] for a in data["actions"] for k in ("activePlayerId", "passivePlayerId") if k in a and a is not goal}
    ids |= {goal["passivePlayerId"]} if "passivePlayerId" in goal else set()

    def doctor(d: dict) -> None:
        d["playerIds"] = []
        next(a for a in d["actions"] if a["type"] == "GOAL")["activePlayerId"] = "999"

    names = envelope([{"id": id_, "name": f"Player {id_}"} for id_ in ids])
    urls = batch_urls("players", ids)
    response, client = game_report("4909471", doctor, dict.fromkeys(urls, names))
    assert [url for url in client.urls if "/players?" in url] == urls
    home = response["home"]
    assert (home["lineup"][0]["name"], home["substitutes"][0]["name"]) == (
        f"Player {home['lineup'][0]['id']}",
        f"Player {home['substitutes'][0]['id']}",
    )
    assert {"id": "999", "name": "Player 999"} in [e["player"] for e in response["events"]]


def test_report_stage_from_attributes_without_embedded_group():
    def doctor(data: dict) -> None:
        del data["baseDetails"]["competitionGroup"]
        data["baseDetails"]["competitionGroupId"] = "1"

    assert game_report("4359338", doctor)[0]["stage"] == "Group 1"


def attributes_data() -> dict:
    return json.loads(fixture_bytes(f"{TMAPI_URL}/attributes"))["data"]


def test_report_malformed_competition_groups_is_502():
    attributes = attributes_data()
    attributes["competitionGroups"] = {}
    with pytest.raises(UpstreamError) as e:
        game_report("4359338", overrides={f"{TMAPI_URL}/attributes": envelope(attributes)})
    assert (e.value.status_code, e.value.url, e.value.reason) == (
        502,
        f"{TMAPI_URL}/attributes",
        "Unexpected tmapi response: attributes.competitionGroups is not a list of {id, name, ...}",
    )


@pytest.mark.parametrize(
    "attributes",
    [
        [],
        {},
        {"actions": {}},
        {"actions": [{"id": 1, "action": "Goal"}, "Assist"]},
        {"actions": [{"id": 1, "action": "Goal"}, {"id": "2", "action": "Assist"}]},
        {"actions": [{"id": 1, "action": "Goal"}, {"id": 2, "action": None}]},
    ],
)
def test_attribute_names_unexpected_shape_is_502(attributes):
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.attribute_names(attributes, "actions", "action")
    assert (e.value.status_code, e.value.url, e.value.reason) == (
        502,
        f"{TMAPI_URL}/attributes",
        "Unexpected tmapi response: attributes.actions is not a list of {id, action}",
    )


def test_attribute_names():
    attributes = {"reasons": [{"id": 1, "reason": "Pass"}, {"id": 2, "reason": "Corner", "type": "GOAL"}]}
    assert TransfermarktGame.attribute_names(attributes, "reasons", "reason") == {1: "Pass", 2: "Corner"}


def test_stadium_without_city():
    content = envelope({"name": " Anfield ", "location": {"city": None}})
    assert TransfermarktGame.parse_stadium(content, stadium_id="35") == {"id": "35", "name": "Anfield", "city": None}


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"<html>", "Unexpected tmapi response: body is not JSON"),
        (envelope({"location": {"city": "Liverpool"}}), "Unexpected tmapi response: no name in stadium 35"),
    ],
)
def test_stadium_unexpected_shape_is_502(content, reason):
    with pytest.raises(UpstreamError) as e:
        TransfermarktGame.parse_stadium(content, stadium_id="35")
    assert (e.value.status_code, e.value.url, e.value.reason) == (502, f"{TMAPI_URL}/stadium/35", reason)
