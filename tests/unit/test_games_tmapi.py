"""Parser tests for the tmapi-backed game report service, on recorded fixtures."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable

import pytest

from app.http import UpstreamError, UpstreamResponse
from app.schemas.games import GameReport
from app.services.games import URL_GAME
from app.services.games_report import TransfermarktGame
from app.tmapi import TMAPI_URL, batch_urls
from tests.unit.tmapi_helpers import RecordingClient, envelope, fixture_bytes


class DoctoredClient(RecordingClient):
    """A RecordingClient that serves `overrides` (url to body, or an UpstreamError to raise) instead of the recorded
    responses."""

    def __init__(self, overrides: dict[str, bytes | UpstreamError]):
        super().__init__()
        self.overrides = overrides

    async def get(self, url: str) -> UpstreamResponse:
        if url in self.overrides:
            self.urls.append(url)
            override = self.overrides[url]
            if isinstance(override, UpstreamError):
                raise override
            return UpstreamResponse(url=url, status_code=200, content=override)
        return await super().get(url)


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
