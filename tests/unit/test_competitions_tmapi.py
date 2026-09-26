"""Parser tests for the tmapi-backed competition table and fixtures services, on recorded fixtures."""

import asyncio
import copy
import json

import pytest

from app.http import UpstreamError, UpstreamResponse
from app.schemas.competitions import CompetitionFixtures, CompetitionTable
from app.services.competitions.fixtures import TransfermarktCompetitionFixtures
from app.services.competitions.table import TransfermarktCompetitionTable
from app.services.games import (
    URL_GAME,
    competition_group_names,
    fetch_group_names,
    fetch_shootout_scores,
    parse_game,
    parse_shootout_score,
    validate_game,
)
from app.tmapi import TMAPI_URL, batch_urls, parse_competition
from tests.unit.tmapi_helpers import NoRequestClient, envelope, fixture_bytes

URL = "https://example.test/fixtures"


def lookups(ids: set[str]) -> list[tuple[str, bytes]]:
    return [(url, fixture_bytes(url)) for url in batch_urls("clubs", ids)]


def table_bytes(competition_id: str, season_id: str | None = None) -> bytes:
    return fixture_bytes(TransfermarktCompetitionTable.table_url(competition_id, season_id))


def table_data(competition_id: str, season_id: str | None = None) -> dict:
    return json.loads(table_bytes(competition_id, season_id))["data"]


def competition_table(competition_id: str, season_id: str | None = None, table: bytes | None = None) -> dict:
    table = table if table is not None else table_bytes(competition_id, season_id)
    tables = TransfermarktCompetitionTable.parse_tables(table, competition_id=competition_id, season_id=season_id)
    tfmkt = TransfermarktCompetitionTable.from_bytes(
        table,
        competition=fixture_bytes(f"{TMAPI_URL}/competition/{competition_id}"),
        clubs=lookups(TransfermarktCompetitionTable.club_ids(tables)),
        competition_id=competition_id,
        season_id=season_id,
    )
    return CompetitionTable.model_validate(tfmkt.get_competition_table()).model_dump(by_alias=True)


def fixtures_data(competition_id: str, season_id: str | None = None) -> dict:
    return json.loads(fixture_bytes(TransfermarktCompetitionFixtures.fixtures_url(competition_id, season_id)))["data"]


def competition_games(
    competition_id: str,
    season_id: str | None = None,
    matchday: int | None = None,
    fixtures: bytes | None = None,
    attributes: bytes | None = None,
) -> list[dict]:
    if fixtures is None:
        fixtures = fixture_bytes(TransfermarktCompetitionFixtures.fixtures_url(competition_id, season_id))
    games = TransfermarktCompetitionFixtures.parse_games(
        fixtures, competition_id=competition_id, season_id=season_id, matchday=matchday
    )
    tfmkt = TransfermarktCompetitionFixtures.from_bytes(
        fixtures,
        competition=fixture_bytes(f"{TMAPI_URL}/competition/{competition_id}"),
        clubs=lookups(TransfermarktCompetitionFixtures.club_ids(games)),
        attributes=attributes,
        games=[
            (url, fixture_bytes(url))
            for url in (
                URL_GAME.format(game_id=g["id"]) for g in games if g["score"]["additionType"] == "after_shootout"
            )
        ],
        competition_id=competition_id,
        season_id=season_id,
        matchday=matchday,
    )
    response = CompetitionFixtures.model_validate(tfmkt.get_competition_fixtures()).model_dump(by_alias=True)
    return response["games"]


def game_by_id(games: list[dict], game_id: str) -> dict:
    (game,) = [g for g in games if g["id"] == game_id]
    return game


def raw_game(data: dict, game_id: str) -> dict:
    (game,) = [g for r in data["fixtures"] for g in r["games"] if g["id"] == game_id]
    return game


# Tables


def test_table_league_rows():
    table = competition_table("ES1", "2024")
    assert (table["id"], table["name"], table["seasonId"]) == ("ES1", "LaLiga", "2024")
    ((name, rows),) = [(t["name"], t["rows"]) for t in table["tables"]]
    assert name == "LaLiga"
    assert len(rows) == 20
    assert [r["position"] for r in rows] == list(range(1, 21))
    assert rows[0] == {
        "clubId": "131",
        "clubName": "FC Barcelona",
        "position": 1,
        "previousPosition": 1,
        "played": 38,
        "won": 28,
        "drawn": 4,
        "lost": 6,
        "goalsFor": 102,
        "goalsAgainst": 39,
        "goalDifference": 63,
        "points": 88,
        "pointsDeducted": None,
        "zone": "Champions & UEFA Champions League",
    }


def test_table_zone_is_none_for_empty_description():
    rows = competition_table("ES1", "2024")["tables"][0]["rows"]
    raw = {c["clubId"]: c for c in table_data("ES1", "2024")["tables"][0]["clubs"]}
    assert raw["681"]["positioning"]["description"] == ""
    (row,) = [r for r in rows if r["clubId"] == "681"]
    assert row["zone"] is None


def test_table_current_season_defaults_to_current_season_id():
    table = competition_table("ES1")
    assert table["seasonId"] == "2026"
    (barcelona,) = [r for r in table["tables"][0]["rows"] if r["clubId"] == "131"]
    assert barcelona["points"] == 21


def test_table_groups():
    tables = competition_table("CLI")["tables"]
    assert [t["name"] for t in tables] == [f"Group {g}" for g in "ABCDEFGH"]
    assert all(len(t["rows"]) == 4 for t in tables)
    assert [t["name"] for t in competition_table("UNLA")["tables"]] == [f"Group {g}" for g in "1234"]


def test_table_cup_has_no_tables():
    assert competition_table("CDR")["tables"] == []


def test_table_points_deduction_and_order():
    data = table_data("ES1", "2024")
    clubs = data["tables"][0]["clubs"]
    clubs[0]["game"]["pointsMinus"] = 3
    clubs.reverse()
    rows = competition_table("ES1", "2024", table=envelope(data))["tables"][0]["rows"]
    assert [r["position"] for r in rows] == list(range(1, 21))
    assert rows[0]["pointsDeducted"] == 3


@pytest.mark.parametrize(
    ("doctor", "message"),
    [
        (lambda d: d.update(tables={}), "no tables list"),
        (
            lambda d: d["tables"][0]["clubs"][0]["game"].update(points="88"),
            "game.points='88' in table 'LaLiga' club 131",
        ),
        (lambda d: d["tables"][0]["clubs"][0]["game"].update(points=True), "game.points=True"),
        (lambda d: d["tables"][0]["clubs"][0]["ranking"].pop("previous"), "no ranking.previous in table 'LaLiga'"),
        (lambda d: d["tables"][0].update(clubs=None), "clubs=None in table 0"),
    ],
)
def test_table_unexpected_shape_is_502(doctor, message):
    data = table_data("ES1", "2024")
    doctor(data)
    with pytest.raises(UpstreamError) as e:
        TransfermarktCompetitionTable.parse_tables(envelope(data), competition_id="ES1", season_id="2024")
    assert e.value.status_code == 502
    assert message in e.value.reason


def test_competition_not_found_envelope_is_404():
    content = b'{"success":false,"message":"Competition not found"}'
    with pytest.raises(UpstreamError) as e:
        parse_competition(f"{TMAPI_URL}/competition/XX", content)
    assert e.value.status_code == 404
    assert "Competition not found" in e.value.reason


def test_competition_unexpected_shape_is_502():
    with pytest.raises(UpstreamError) as e:
        parse_competition(f"{TMAPI_URL}/competition/ES1", envelope({"name": "LaLiga", "currentSeasonId": "2026"}))
    assert e.value.status_code == 502


def test_table_failure_envelope_is_502():
    with pytest.raises(UpstreamError) as e:
        TransfermarktCompetitionTable.parse_tables(b'{"success":false,"message":"x"}', competition_id="ES1")
    assert e.value.status_code == 502


# Fixtures and games


def test_fixtures_league_games():
    games = competition_games("ES1", "2024")
    assert len(games) == 380
    keys = [(g["matchday"], g["date"], g["id"]) for g in games]
    assert keys == sorted(keys)
    assert {g["stage"] for g in games} == {None}
    assert {(g["seasonId"], g["seasonName"], g["competitionName"]) for g in games} == {("2024", "24/25", "LaLiga")}


def test_fixtures_matchday_filter():
    games = competition_games("ES1", "2024", matchday=1)
    raw = [
        g["id"]
        for r in fixtures_data("ES1", "2024")["fixtures"]
        for g in r["games"]
        if g["baseDetails"]["gameDay"] == 1
    ]
    assert sorted(g["id"] for g in games) == sorted(raw)
    assert len(games) == 10
    assert competition_games("ES1", "2024", matchday=99) == []


def test_game_stage_from_embedded_group():
    games = competition_games("EURO", "2023")
    assert game_by_id(games, "4235807")["stage"] == "Group A"
    assert game_by_id(games, "4359338")["stage"] == "Quarter-Finals"


def test_game_stage_from_attributes_when_group_is_not_embedded():
    data = fixtures_data("EURO", "2023")
    del raw_game(data, "4235807")["baseDetails"]["competitionGroup"]
    raw_game(data, "4359338")["baseDetails"].pop("competitionGroup")
    raw_game(data, "4359338")["baseDetails"]["competitionGroupId"] = "ZZ-unknown"
    games = competition_games(
        "EURO", "2023", fixtures=envelope(data), attributes=fixture_bytes(f"{TMAPI_URL}/attributes")
    )
    assert game_by_id(games, "4235807")["stage"] == "Group A"
    assert game_by_id(games, "4359338")["stage"] == "ZZ-unknown"


def test_game_ended_after():
    games = competition_games("EURO", "2023")
    assert game_by_id(games, "4235807")["endedAfter"] == "regular"
    assert game_by_id(games, "4359331")["endedAfter"] == "extra_time"
    assert [g["endedAfter"] for g in games].count("shootout") == 3
    assert [g["id"] for g in games if g["shootout"] is not None] == ["4359332", "4359337", "4359338"]


def test_game_ended_after_is_none_until_finished():
    games = competition_games("CDR")
    assert {(g["isFinished"], g["endedAfter"]) for g in games} == {(False, None)}


# tmapi's score of a shootout game includes the shootout goals (6-4 for England 1-1 Switzerland, 5-3 on penalties).
@pytest.mark.parametrize(
    ("game_id", "goals", "shootout"),
    [
        ("4359338", (1, 1), {"home": 5, "away": 3}),  # England 1-1 Switzerland: GOAL actions at 75' and 80'
        ("4359332", (0, 0), {"home": 3, "away": 0}),  # Portugal 0-0 Slovenia: no GOAL actions
        ("4359337", (0, 0), {"home": 3, "away": 5}),  # Portugal 0-0 France: no GOAL actions
    ],
)
def test_game_shootout_goals_are_separated(game_id, goals, shootout):
    game = game_by_id(competition_games("EURO", "2023"), game_id)
    assert game["endedAfter"] == "shootout"
    assert (game["homeGoals"], game["awayGoals"]) == goals
    assert game["shootout"] == shootout


def game_report(game_id: str) -> dict:
    return json.loads(fixture_bytes(URL_GAME.format(game_id=game_id)))["data"]


def shootout_score(game_id: str, report: dict | None = None, **score: int) -> tuple[int, int]:
    game = copy.deepcopy(raw_game(fixtures_data("EURO", "2023"), game_id))
    game["score"].update(score)
    report = report if report is not None else game_report(game_id)
    return parse_shootout_score(URL_GAME.format(game_id=game_id), envelope(report), game=game)


def test_shootout_regular_score_is_the_last_goal_score():
    assert shootout_score("4359338") == (5, 3)
    report = game_report("4359338")
    goals = [a for a in report["actions"] if a["type"] == "GOAL"]
    assert [(a["minute"], a["score"]) for a in goals] == [(80, {"home": 1, "away": 1}), (75, {"home": 0, "away": 1})]
    report["actions"].reverse()
    assert shootout_score("4359338", report) == (5, 3)


def test_shootout_regular_score_is_ordered_by_minute_before_total():
    report = game_report("4359338")
    early = next(a for a in report["actions"] if a["type"] == "GOAL" and a["minute"] == 75)
    early["score"] = {"home": 2, "away": 2}
    assert shootout_score("4359338", report) == (5, 3)
    early["addedTime"] = 6
    early["minute"] = 80
    assert shootout_score("4359338", report) == (4, 2)


def test_shootout_regular_score_tie_on_minute_takes_the_higher_total():
    report = game_report("4359338")
    goals = [a for a in report["actions"] if a["type"] == "GOAL"]
    goals[0]["minute"] = goals[1]["minute"] = 90
    goals[1]["addedTime"] = goals[0]["addedTime"]
    goals[1]["score"] = {"home": 1, "away": 0}  # a lower total than 1-1 but a higher home-minus-away difference
    assert shootout_score("4359338", report) == (5, 3)
    report["actions"].reverse()
    assert shootout_score("4359338", report) == (5, 3)


def test_shootout_regular_score_without_goal_actions_is_0_0():
    assert not [a for a in game_report("4359332")["actions"] if a["type"] == "GOAL"]
    assert shootout_score("4359332") == (3, 0)


@pytest.mark.parametrize(
    ("doctor", "score", "message"),
    [
        (lambda r: r.update(actions={}), {}, "actions={} in game 4359338"),
        (lambda r: r["actions"].append("GOAL"), {}, "no type in game 4359338 action"),
        (
            lambda r: next(a for a in r["actions"] if a["type"] == "GOAL").pop("score"),
            {},
            "no score.home in game 4359338 GOAL action",
        ),
        (
            lambda r: next(a for a in r["actions"] if a["type"] == "GOAL").update(score=None),
            {},
            "no score.home in game 4359338 GOAL action",
        ),
        (lambda r: None, {"home": None}, "score.home=None in game 4359338"),
        (lambda r: None, {"home": 0}, "shootout score -1-3 in game 4359338"),
        (lambda r: None, {"home": 1, "away": 1}, "shootout score 0-0 in game 4359338"),
    ],
)
def test_shootout_unexpected_shape_is_502(doctor, score, message):
    report = game_report("4359338")
    doctor(report)
    with pytest.raises(UpstreamError) as e:
        shootout_score("4359338", report, **score)
    assert e.value.status_code == 502
    assert message in e.value.reason
    assert e.value.url == URL_GAME.format(game_id="4359338")


@pytest.mark.parametrize("score", [{"home": 1, "away": 2}, {"home": 2, "away": 1}])
def test_shootout_with_one_goal_and_a_side_without_goals_is_plausible(score):
    # The last GOAL action is 1-1, so the shootout is 0-1 or 1-0: the lowest result the check accepts.
    assert shootout_score("4359338", **score) == (score["home"] - 1, score["away"] - 1)


def test_game_ended_after_passes_unknown_addition_type_through():
    game = copy.deepcopy(raw_game(fixtures_data("EURO", "2023"), "4359338"))
    game["score"]["additionType"] = "after_golden_goal"
    validate_game(URL, game)
    parsed = parse_game(
        game,
        competition_names={"EURO": "UEFA Euro"},
        club_names={"3299": "England", "3384": "Switzerland"},
        group_names={},
        shootout_scores={},
    )
    assert parsed["endedAfter"] == "after_golden_goal"
    assert parsed["shootout"] is None


def test_game_without_kick_off_time_and_score():
    game = game_by_id(competition_games("CDR"), "5049765")
    assert game["isTimeDefined"] is False
    assert (game["homeGoals"], game["awayGoals"], game["attendance"]) == (None, None, None)
    assert game["date"].isoformat() == "2026-10-04T12:00:00+00:00"
    assert game["stage"] == "Qualifying Round 2nd leg"


@pytest.mark.parametrize(
    ("doctor", "message"),
    [
        (lambda g: g["score"].update(home="6"), "score.home='6' in game 4359338"),
        (lambda g: g["score"].pop("away"), "no score.away in game 4359338"),
        (lambda g: g["baseDetails"].update(gameDay=True), "baseDetails.gameDay=True in game 4359338"),
        (
            lambda g: g["baseDetails"]["competitionGroup"].update(name=None),
            "competitionGroup.name=None in game 4359338",
        ),
        (lambda g: g["baseDetails"]["date"].update(dateTimeUTC="2024-07-06T16:00:00"), "dateTimeUTC='2024-07-06T16"),
        (lambda g: g["baseDetails"]["date"].update(dateTimeUTC="soon"), "dateTimeUTC='soon' in game 4359338"),
    ],
)
def test_game_unexpected_shape_is_502(doctor, message):
    data = fixtures_data("EURO", "2023")
    doctor(raw_game(data, "4359338"))
    with pytest.raises(UpstreamError) as e:
        TransfermarktCompetitionFixtures.parse_games(envelope(data), competition_id="EURO", season_id="2023")
    assert e.value.status_code == 502
    assert message in e.value.reason
    assert e.value.url == TransfermarktCompetitionFixtures.fixtures_url("EURO", "2023")


def test_game_that_is_not_an_object_is_502():
    url = TransfermarktCompetitionFixtures.fixtures_url("ES1", None)
    with pytest.raises(UpstreamError) as e:
        TransfermarktCompetitionFixtures.parse_games(
            envelope({"fixtures": [{"games": ["4359338"]}]}), competition_id="ES1"
        )
    assert (e.value.status_code, e.value.detail) == (
        502,
        f"Unexpected tmapi response: no id in game None for url: {url}",
    )


def test_fixtures_unexpected_shape_is_502():
    for data in ({"fixtures": {}}, {"fixtures": [{"gameDay": 1}]}, []):
        with pytest.raises(UpstreamError) as e:
            TransfermarktCompetitionFixtures.parse_games(envelope(data), competition_id="ES1")
        assert e.value.status_code == 502


def test_group_names_are_only_fetched_when_a_group_is_not_embedded():
    games = [g for r in fixtures_data("EURO", "2023")["fixtures"] for g in r["games"]]
    assert asyncio.run(fetch_group_names(NoRequestClient(), games)) == {}  # type: ignore[arg-type]

    del games[0]["baseDetails"]["competitionGroup"]
    client = NoRequestClient()
    client.tmapi_attributes = json.loads(fixture_bytes(f"{TMAPI_URL}/attributes"))["data"]
    assert asyncio.run(fetch_group_names(client, games))["A"] == "Group A"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "attributes",
    [
        [],
        {},
        {"competitionGroups": {}},
        {"competitionGroups": [{"id": "A", "name": "Group A"}, {"id": "B"}]},
        {"competitionGroups": [{"id": "A", "name": "Group A"}, {"name": "Group B"}]},
        {"competitionGroups": [{"id": "A", "name": "Group A"}, "B"]},
    ],
)
def test_competition_group_names_rejects_anything_but_a_list_of_id_and_name(attributes):
    with pytest.raises(UpstreamError) as e:
        competition_group_names(URL, attributes)
    assert (e.value.status_code, e.value.detail) == (
        502,
        f"Unexpected tmapi response: attributes.competitionGroups is not a list of {{id, name, ...}} for url: {URL}",
    )


def test_group_names_error_names_the_attributes_url():
    games = [g for r in fixtures_data("EURO", "2023")["fixtures"] for g in r["games"]]
    del games[0]["baseDetails"]["competitionGroup"]
    client = NoRequestClient()
    client.tmapi_attributes = {"competitionGroups": None}
    with pytest.raises(UpstreamError) as e:
        asyncio.run(fetch_group_names(client, games))  # type: ignore[arg-type]
    assert e.value.url == f"{TMAPI_URL}/attributes"


class FixtureClient:
    """A client that serves recorded /game/{id} reports and records the requested URLs."""

    def __init__(self):
        self.urls: list[str] = []

    async def get(self, url: str) -> UpstreamResponse:
        assert url.startswith(f"{TMAPI_URL}/game/"), url
        self.urls.append(url)
        return UpstreamResponse(url=url, status_code=200, content=fixture_bytes(url))


def test_game_reports_are_only_fetched_for_finished_shootout_games():
    games = [g for r in fixtures_data("EURO", "2023")["fixtures"] for g in r["games"]]
    client = FixtureClient()
    scores = asyncio.run(fetch_shootout_scores(client, games))  # type: ignore[arg-type]
    assert scores == {"4359332": (3, 0), "4359337": (3, 5), "4359338": (5, 3)}
    assert client.urls == [URL_GAME.format(game_id=i) for i in ("4359332", "4359337", "4359338")]

    raw_game({"fixtures": [{"games": games}]}, "4359338")["isFinished"] = False
    client = FixtureClient()
    assert set(asyncio.run(fetch_shootout_scores(client, games))) == {"4359332", "4359337"}  # type: ignore[arg-type]

    league = [g for r in fixtures_data("ES1", "2024")["fixtures"] for g in r["games"]]
    assert asyncio.run(fetch_shootout_scores(NoRequestClient(), league)) == {}  # type: ignore[arg-type]


def test_game_report_error_names_the_report_url():
    class BrokenReportClient:
        async def get(self, url: str) -> UpstreamResponse:
            return UpstreamResponse(url=url, status_code=200, content=b"[]")

    games = [g for r in fixtures_data("EURO", "2023")["fixtures"] for g in r["games"]]
    with pytest.raises(UpstreamError) as e:
        asyncio.run(fetch_shootout_scores(BrokenReportClient(), games))  # type: ignore[arg-type]
    assert e.value.detail == f"Unexpected tmapi response: no success flag for url: {URL_GAME.format(game_id='4359332')}"
