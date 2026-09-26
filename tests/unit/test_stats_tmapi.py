"""Tests of the tmapi-backed player stats service on doctored responses: aggregation, goalkeeper games, errors."""

import asyncio
import json
from typing import Any

import pytest
from fastapi import HTTPException

from app.http import UpstreamError
from app.services.players.stats import TransfermarktPlayerStats
from app.tmapi import TMAPI_URL
from tests.unit.tmapi_helpers import DoctoredClient, envelope, fixture_bytes

PERFORMANCE_URL = f"{TMAPI_URL}/player/937958/performance-game"
PLAYER_URL = f"{TMAPI_URL}/player/937958"
ATTRIBUTES_URL = f"{TMAPI_URL}/attributes"
POSITIONS = {1: "Goalkeeper", 12: "Attack"}


def game(
    game_id: str,
    *,
    season: int = 2024,
    competition: str = "ES1",
    club: str = "131",
    state: str = "played",
    position: int | None = 12,
    minutes: int = 90,
    goals: int = 0,
    assists: int = 0,
    own_goals: int = 0,
    penalty_goals: int = 0,
    on_pitch_conceded: int = 0,
    opponent_goals: int = 0,
    yellow: int = 0,
    yellow_red: dict | None = None,
    red: dict | None = None,
) -> dict[str, Any]:
    """A performance-game row with the fields the service reads."""
    return {
        "gameInformation": {
            "gameId": game_id,
            "seasonId": season,
            "competitionId": competition,
            "season": {"display": f"{season % 100}/{season % 100 + 1}"},
        },
        "clubsInformation": {"club": {"clubId": club, "opponentGoalsTotal": opponent_goals}},
        "statistics": {
            "generalStatistics": {"participationState": state, "positionId": position},
            "playingTimeStatistics": {"playedMinutes": minutes},
            "goalStatistics": {
                "goalsScoredTotal": goals,
                "assists": assists,
                "ownGoalsScored": own_goals,
                "penaltyShooterGoalsScored": penalty_goals,
                "opponentGoalsOnThePitch": on_pitch_conceded,
            },
            "cardStatistics": {"yellowCardNet": yellow, "yellowRedCard": yellow_red, "redCard": red},
        },
    }


def stats(rows: list[dict], main_position_id: int | None = 12) -> list[dict]:
    tfmkt = TransfermarktPlayerStats(
        player_id="937958",
        performance=rows,
        competition_names={"ES1": "LaLiga"},
        club_names={"131": "FC Barcelona"},
        position_categories=POSITIONS,
        main_position_id=main_position_id,
    )
    return tfmkt.get_player_stats()["stats"]


CARD = {"minute": 70, "minuteOvertime": 0, "actionId": 301}


def test_stats_sum_and_count_every_played_game_of_a_group():
    rows = [
        game("1", minutes=90, goals=2, assists=1, own_goals=1, penalty_goals=1, yellow=1, yellow_red=CARD),
        game("2", minutes=80, goals=1, assists=2, own_goals=2, penalty_goals=0, yellow=0, yellow_red=CARD, red=CARD),
        game("3", state="injured", own_goals=5, yellow_red=CARD),
        game("4", minutes=10, goals=0, assists=0, own_goals=0, penalty_goals=1, yellow=1),
    ]
    assert stats(rows) == [
        {
            "seasonId": "2024",
            "seasonName": "24/25",
            "competitionId": "ES1",
            "competitionName": "LaLiga",
            "clubId": "131",
            "clubName": "FC Barcelona",
            "appearances": 3,
            "goals": 3,
            "assists": 3,
            "ownGoals": 3,
            "penaltyGoals": 2,
            "yellowCards": 2,
            "secondYellowCards": 2,
            "redCards": 1,
            "minutesPlayed": 180,
            "goalsConceded": None,
            "cleanSheets": None,
        }
    ]


def goalkeeper_stats(rows: list[dict], main_position_id: int | None) -> tuple[Any, Any]:
    (entry,) = stats(rows, main_position_id=main_position_id)
    return entry["goalsConceded"], entry["cleanSheets"]


def test_stats_a_game_positioned_in_the_table_ignores_the_main_position():
    # A goalkeeper fielded as a forward: that game's position decides, not the main position.
    rows = [game("1", position=12, on_pitch_conceded=2, opponent_goals=2)]
    assert goalkeeper_stats(rows, main_position_id=1) == (None, None)
    rows.append(game("2", position=1, on_pitch_conceded=1, opponent_goals=1))
    rows.append(game("3", position=1, on_pitch_conceded=0, opponent_goals=0))
    assert goalkeeper_stats(rows, main_position_id=12) == (1, 1)


@pytest.mark.parametrize("position", [0, None, 99])
def test_stats_a_game_without_a_known_position_falls_back_to_the_main_position(position):
    rows = [game("1", position=position, on_pitch_conceded=3, opponent_goals=3)]
    assert goalkeeper_stats(rows, main_position_id=None) == (None, None)
    assert goalkeeper_stats(rows, main_position_id=12) == (None, None)
    assert goalkeeper_stats(rows, main_position_id=1) == (3, 0)


def parse_performance(rows: Any) -> list[dict]:
    body = envelope({"performance": rows})
    return TransfermarktPlayerStats.parse_performance(body, player_id="937958")


def upstream_error(call) -> UpstreamError:
    with pytest.raises(UpstreamError) as e:
        call()
    return e.value


@pytest.mark.parametrize(
    ("doctor", "reason"),
    [
        # A played game gets the base checks too, not only the played-game ones.
        (
            lambda row: row["gameInformation"].update(seasonId="2024"),
            "gameInformation.seasonId='2024' in game 7",
        ),
        (
            lambda row: row["statistics"]["generalStatistics"].update(positionId="1"),
            "statistics.generalStatistics.positionId='1' in game 7",
        ),
        (
            lambda row: row.pop("statistics"),
            "statistics.generalStatistics.participationState=None in game 7",
        ),
    ],
)
def test_parse_performance_names_the_offending_path_and_game(doctor, reason):
    rows = [game("6"), game("7")]
    doctor(rows[1])
    error = upstream_error(lambda: parse_performance(rows))
    assert (error.status_code, error.url, error.reason) == (
        502,
        PERFORMANCE_URL,
        f"Unexpected tmapi response: {reason}",
    )


def test_parse_performance_accepts_a_played_game_without_a_position():
    rows = [game("1", position=None), game("2", position=0)]
    assert parse_performance(rows) == rows


def test_parse_performance_reraises_a_bad_envelope_with_the_performance_url():
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_performance(b"<html>", player_id="937958"))
    assert (error.status_code, error.url) == (502, PERFORMANCE_URL)


def test_parse_performance_rejects_data_without_a_performance_list():
    body = envelope([])
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_performance(body, player_id="937958"))
    assert (error.status_code, error.url, error.reason) == (
        502,
        PERFORMANCE_URL,
        "Unexpected tmapi response: no performance list",
    )


@pytest.mark.parametrize(
    "positions",
    [
        [{"id": 1, "category": "Goalkeeper"}, {"id": "2", "category": "Defender"}],
        [{"id": 1, "category": "Goalkeeper"}, {"id": 2, "category": None}],
        [{"id": 1, "category": "Goalkeeper"}, "Defender"],
    ],
)
def test_position_categories_reject_a_malformed_position(positions):
    body = envelope({"positions": positions})
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_position_categories(body))
    assert (error.status_code, error.url, error.reason) == (
        502,
        ATTRIBUTES_URL,
        "Unexpected tmapi response: attributes.positions is not a list of {id, category, ...}",
    )


def test_position_categories_reject_attributes_that_are_not_an_object():
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_position_categories(envelope([])))
    assert error.reason == "Unexpected tmapi response: attributes.positions is not a list of {id, category, ...}"


def test_position_categories_require_a_goalkeeper_position():
    body = envelope({"positions": [{"id": 2, "category": "Defender"}, {"id": 12, "category": "Attack"}]})
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_position_categories(body))
    assert (error.status_code, error.url, error.reason) == (
        502,
        ATTRIBUTES_URL,
        "Unexpected tmapi response: no position with category Goalkeeper",
    )


def test_position_categories_report_the_attributes_url_for_a_bad_envelope():
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_position_categories(b"<html>"))
    assert (error.status_code, error.url) == (502, ATTRIBUTES_URL)


@pytest.mark.parametrize(("attributes", "expected"), [({"positionId": 1}, 1), ({"positionId": None}, None)])
def test_parse_main_position(attributes, expected):
    body = envelope({"attributes": attributes})
    assert TransfermarktPlayerStats.parse_main_position(body, player_id="937958") == expected


@pytest.mark.parametrize(
    ("data", "shown"),
    [
        ([], "None"),
        ({"attributes": None}, "None"),
        ({"attributes": {"positionId": True}}, "True"),
        ({"attributes": {"positionId": "1"}}, "'1'"),
    ],
)
def test_parse_main_position_rejects_a_malformed_player(data, shown):
    body = envelope(data)
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_main_position(body, player_id="937958"))
    assert (error.status_code, error.url, error.reason) == (
        502,
        PLAYER_URL,
        f"Unexpected tmapi response: attributes.positionId={shown}",
    )


def test_parse_main_position_reports_the_player_url_for_a_bad_envelope():
    error = upstream_error(lambda: TransfermarktPlayerStats.parse_main_position(b"<html>", player_id="937958"))
    assert (error.status_code, error.url) == (502, PLAYER_URL)


def fetch(overrides: dict[str, bytes | UpstreamError]) -> TransfermarktPlayerStats:
    client = DoctoredClient(overrides)
    return asyncio.run(TransfermarktPlayerStats.fetch(client, player_id="937958"))  # type: ignore[arg-type]


def from_bytes(**overrides: Any) -> TransfermarktPlayerStats:
    kwargs: dict[str, Any] = {
        "player": envelope({"attributes": {"positionId": 12}}),
        "attributes": fixture_bytes(ATTRIBUTES_URL),
        "competitions": [("https://tmapi.example/competitions", envelope([{"id": "ES1", "name": "LaLiga"}]))],
        "clubs": [("https://tmapi.example/clubs", envelope([{"id": "131", "name": "FC Barcelona"}]))],
        **overrides,
    }
    performance = kwargs.pop("performance", envelope({"performance": [game("1")]}))
    return TransfermarktPlayerStats.from_bytes(performance, player_id="937958", **kwargs)


NOT_FOUND = b'{"success": false, "message": "Playerperformancegame not found"}'


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: fetch({PERFORMANCE_URL: NOT_FOUND}), id="fetch"),
        pytest.param(lambda: from_bytes(performance=NOT_FOUND), id="from_bytes"),
    ],
)
def test_unknown_player_detail_names_the_performance_url(build):
    with pytest.raises(HTTPException) as e:
        build()
    assert (e.value.status_code, e.value.detail) == (404, f"Invalid request (url: {PERFORMANCE_URL})")


def test_fetch_reports_the_attributes_url_for_attributes_without_a_goalkeeper():
    error = upstream_error(
        lambda: fetch({ATTRIBUTES_URL: envelope({"positions": [{"id": 2, "category": "Defender"}]})})
    )
    assert (error.url, error.reason) == (
        ATTRIBUTES_URL,
        "Unexpected tmapi response: no position with category Goalkeeper",
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda body: fetch({PLAYER_URL: body}), id="fetch"),
        pytest.param(lambda body: from_bytes(player=body), id="from_bytes"),
    ],
)
def test_malformed_player_reports_the_player_url(build):
    error = upstream_error(lambda: build(envelope({"attributes": {"positionId": "1"}})))
    assert (error.url, error.reason) == (PLAYER_URL, "Unexpected tmapi response: attributes.positionId='1'")


@pytest.mark.parametrize("lookup", ["competitions", "clubs"])
def test_from_bytes_reports_the_lookup_url_for_a_malformed_lookup(lookup):
    url = f"https://tmapi.example/{lookup}?ids[]=1"
    error = upstream_error(lambda: from_bytes(**{lookup: [(url, envelope({"id": "1"}))]}))
    assert (error.url, error.reason) == (url, "Unexpected tmapi response: lookup data is not a list of {id, name}")


def test_from_bytes_builds_the_stats_from_the_given_responses():
    body = envelope({"performance": [game("1", season=2023, goals=1), game("2", season=2024, goals=2)]})
    response = from_bytes(performance=body).get_player_stats()
    assert response["id"] == "937958"
    assert [(s["seasonId"], s["goals"]) for s in response["stats"]] == [("2024", 2), ("2023", 1)]
    assert json.dumps(response)
