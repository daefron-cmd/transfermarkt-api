"""Parser tests for the tmapi-backed club fixtures and squad services, on recorded fixtures."""

import asyncio
import json

import pytest

from app.http import UpstreamError
from app.schemas.clubs import ClubFixtures, ClubSquadMembers
from app.services.clubs.fixtures import TransfermarktClubFixtures
from app.services.clubs.squad import TransfermarktClubSquad
from app.services.games import URL_GAME
from app.tmapi import TMAPI_URL, batch_urls, parse_club
from tests.unit.tmapi_helpers import DoctoredClient, RecordingClient, envelope, fixture_bytes

ATTRIBUTES_URL = f"{TMAPI_URL}/attributes"
NOT_FOUND = b'{"success": false, "message": "Club not found"}'


def club_url(club_id: str) -> str:
    return f"{TMAPI_URL}/club/{club_id}"


# Fixtures


def fixtures_data(club_id: str, season_id: str | None = None) -> dict:
    return json.loads(fixture_bytes(TransfermarktClubFixtures.fixtures_url(club_id, season_id)))["data"]


def fixtures_lookups(club_id: str, fixtures: bytes, season_id: str | None = None) -> dict:
    """The recorded clubs and competitions lookups of the games in `fixtures`, as from_bytes arguments."""
    games = TransfermarktClubFixtures.parse_games(fixtures, club_id=club_id, season_id=season_id)
    club_ids = {game[side]["clubId"] for game in games for side in ("homeClub", "awayClub")}
    competition_ids = {game["baseDetails"]["competitionId"] for game in games}
    return {
        "clubs": [(url, fixture_bytes(url)) for url in batch_urls("clubs", club_ids)],
        "competitions": [(url, fixture_bytes(url)) for url in batch_urls("competitions", competition_ids)],
    }


def club_fixtures(
    club_id: str,
    season_id: str | None = None,
    fixtures: bytes | None = None,
    data_season_id: str | None = None,
    attributes: bytes | None = None,
    games: list[tuple[str, bytes]] | None = None,
) -> dict:
    """The fixtures response built from recorded bytes (or `fixtures`, doctored data of `data_season_id`)."""
    if fixtures is None:
        fixtures = fixture_bytes(TransfermarktClubFixtures.fixtures_url(club_id, data_season_id or season_id))
    tfmkt = TransfermarktClubFixtures.from_bytes(
        fixtures,
        club=fixture_bytes(club_url(club_id)),
        **fixtures_lookups(club_id, fixtures, season_id),
        attributes=attributes,
        games=games or (),
        club_id=club_id,
        season_id=season_id,
    )
    return ClubFixtures.model_validate(tfmkt.get_club_fixtures()).model_dump(by_alias=True)


def game_by_id(response: dict, game_id: str) -> dict:
    (game,) = [g for g in response["games"] if g["id"] == game_id]
    return game


def raw_game(data: dict, game_id: str) -> dict:
    (game,) = [g for g in data["games"] if g["id"] == game_id]
    return game


def test_fixtures_club_games():
    response = club_fixtures("131", "2024")
    assert (response["id"], response["name"], response["seasonId"]) == ("131", "FC Barcelona", "2024")
    games = response["games"]
    assert len(games) == 60
    assert [(g["date"], g["id"]) for g in games] == sorted((g["date"], g["id"]) for g in games)
    assert {g["competitionName"] for g in games} == {"LaLiga", "UEFA Champions League", "Copa del Rey", "Supercopa"}
    assert all("131" in (g["homeClub"]["id"], g["awayClub"]["id"]) for g in games)


@pytest.mark.parametrize(
    ("game_id", "venue", "result", "score"),
    [
        ("4588078", "home", "W", (3, 2)),  # Copa del Rey final: Barcelona 3-2 Real Madrid after extra time
        ("4557608", "away", "W", (0, 1)),  # Atlético 0-1 Barcelona
        ("4445152", "away", "L", (2, 1)),  # Monaco 2-1 Barcelona
        ("4407649", "away", "D", (2, 2)),  # Celta 2-2 Barcelona
    ],
)
def test_fixtures_venue_and_result(game_id, venue, result, score):
    game = game_by_id(club_fixtures("131", "2024"), game_id)
    assert (game["venue"], game["result"], (game["homeGoals"], game["awayGoals"])) == (venue, result, score)


def test_fixtures_copa_del_rey_final():
    game = game_by_id(club_fixtures("131", "2024"), "4588078")
    assert game["date"].isoformat() == "2025-04-26T20:00:00+00:00"
    assert (game["competitionName"], game["stage"], game["endedAfter"]) == ("Copa del Rey", "Final", "extra_time")
    assert (game["homeClub"]["name"], game["awayClub"]["name"]) == ("FC Barcelona", "Real Madrid")


def test_fixtures_result_is_none_until_finished():
    data = fixtures_data("131")
    unfinished = [g for g in data["games"] if not g["isFinished"]]
    assert unfinished and all("gameResult" not in g["score"] for g in unfinished)
    games = club_fixtures("131")["games"]
    assert {(g["isFinished"], g["result"]) for g in games} == {(True, "W"), (False, None)}

    # A result on an unfinished game is not returned; a finished game without one has no result.
    raw_game(data, unfinished[0]["id"])["score"]["gameResult"] = "W"
    finished = next(g for g in data["games"] if g["isFinished"])
    finished["score"]["gameResult"] = None
    response = club_fixtures("131", fixtures=envelope(data))
    assert game_by_id(response, unfinished[0]["id"])["result"] is None
    assert game_by_id(response, finished["id"])["result"] is None


@pytest.mark.parametrize(
    ("doctor", "message"),
    [
        (lambda g: g["score"].update(gameResult="X"), "score.gameResult='X' in game 4588078"),
        (lambda g: g["score"].update(gameResult="w"), "score.gameResult='w' in game 4588078"),
        (lambda g: g["homeClub"].update(clubId="1"), "club 131 did not play game 4588078"),
        (lambda g: g["score"].pop("home"), "no score.home in game 4588078"),
    ],
)
def test_fixtures_unexpected_game_is_502(doctor, message):
    data = fixtures_data("131", "2024")
    doctor(raw_game(data, "4588078"))
    with pytest.raises(UpstreamError) as e:
        TransfermarktClubFixtures.parse_games(envelope(data), club_id="131", season_id="2024")
    assert (e.value.status_code, e.value.url) == (502, f"{TMAPI_URL}/club/131/fixtures?season=2024")
    assert e.value.reason == f"Unexpected tmapi response: {message}"


def test_fixtures_unexpected_shape_is_502():
    for content, reason in [
        (envelope({"games": {}}), "Unexpected tmapi response: no games list"),
        (envelope({"fixtures": []}), "Unexpected tmapi response: no games list"),
        (envelope([]), "Unexpected tmapi response: no games list"),
        (b"<html>", "Unexpected tmapi response: body is not JSON"),
    ]:
        with pytest.raises(UpstreamError) as e:
            TransfermarktClubFixtures.parse_games(content, club_id="131")
        assert (e.value.status_code, e.value.url, e.value.reason) == (502, f"{TMAPI_URL}/club/131/fixtures", reason)


def test_fixtures_season_id_is_the_requested_one():
    # Spain's 2024 fixtures hold games of seasons 2023 (Euro 2024, friendlies) and 2024 (Nations League).
    response = club_fixtures("3375", "2024")
    assert response["seasonId"] == "2024"
    assert {g["seasonId"] for g in response["games"]} == {"2023", "2024"}
    assert len(response["games"]) == 17


def test_fixtures_season_id_is_derived_from_the_games():
    assert club_fixtures("131")["seasonId"] == "2026"
    assert club_fixtures("3375")["seasonId"] == "2026"


def test_fixtures_season_id_is_none_when_games_disagree_or_there_are_none():
    assert club_fixtures("3375", data_season_id="2024")["seasonId"] is None
    response = club_fixtures("131", fixtures=envelope({"games": []}))
    assert (response["seasonId"], response["games"]) == (None, [])


# Spain's 2024 fixtures have no shootout game and embed every stage, so both are doctored in: tmapi's score of a
# shootout game includes the shootout goals, and the report served for it decides the score before the shootout.
SHOOTOUT_REPORTS = {
    # 6-4 after a 1-1 (the recorded report of England 1-1 Switzerland): 5-3 on penalties.
    URL_GAME.format(game_id="4359336"): fixture_bytes(URL_GAME.format(game_id="4359338")),
    # 3-0 after a 0-0 (the recorded report of Portugal 0-0 Slovenia, without GOAL actions): 3-0 on penalties.
    URL_GAME.format(game_id="4359330"): fixture_bytes(URL_GAME.format(game_id="4359332")),
}


def doctored_spain_fixtures() -> bytes:
    data = fixtures_data("3375", "2024")
    raw_game(data, "4359336")["score"].update(home=6, away=4, additionType="after_shootout")
    raw_game(data, "4359330")["score"].update(home=3, away=0, additionType="after_shootout")
    del raw_game(data, "4235809")["baseDetails"]["competitionGroup"]  # group "B"
    del raw_game(data, "4359340")["baseDetails"]["competitionGroup"]  # group "HF"
    return envelope(data)


def shootouts_and_stages(response: dict) -> dict:
    return {
        g["id"]: (g["homeGoals"], g["awayGoals"], g["endedAfter"], g["shootout"], g["stage"])
        for g in response["games"]
        if g["id"] in ("4359336", "4359330", "4235809", "4359340")
    }


SHOOTOUTS_AND_STAGES = {
    "4359336": (1, 1, "shootout", {"home": 5, "away": 3}, "Quarter-Finals"),
    "4359330": (0, 0, "shootout", {"home": 3, "away": 0}, "Round of 16"),
    "4235809": (3, 0, "regular", None, "Group B"),
    "4359340": (2, 1, "regular", None, "Semi-Finals"),
}


def test_fixtures_shootouts_and_stages_from_attributes():
    response = club_fixtures(
        "3375",
        "2024",
        fixtures=doctored_spain_fixtures(),
        attributes=fixture_bytes(ATTRIBUTES_URL),
        games=list(SHOOTOUT_REPORTS.items()),
    )
    assert shootouts_and_stages(response) == SHOOTOUTS_AND_STAGES


def test_fixtures_stage_without_attributes_is_the_group_id():
    response = club_fixtures("3375", "2024", fixtures=doctored_spain_fixtures(), games=list(SHOOTOUT_REPORTS.items()))
    stages = {game_id: stage for game_id, (*_, stage) in shootouts_and_stages(response).items()}
    assert stages == {"4359336": "Quarter-Finals", "4359330": "Round of 16", "4235809": "B", "4359340": "HF"}


def test_fixtures_fetch_shootouts_and_stages():
    fixtures_url = f"{TMAPI_URL}/club/3375/fixtures?season=2024"
    client = DoctoredClient({fixtures_url: doctored_spain_fixtures(), **SHOOTOUT_REPORTS})
    tfmkt = asyncio.run(TransfermarktClubFixtures.fetch(client, club_id="3375", season_id="2024"))  # type: ignore[arg-type]
    assert client.urls[:2] == [club_url("3375"), fixtures_url]
    # /attributes, then the game reports in tmapi's game order.
    assert client.urls[-3:] == [ATTRIBUTES_URL, URL_GAME.format(game_id="4359330"), URL_GAME.format(game_id="4359336")]
    response = ClubFixtures.model_validate(tfmkt.get_club_fixtures()).model_dump(by_alias=True)
    assert shootouts_and_stages(response) == SHOOTOUTS_AND_STAGES


SPAIN_LOOKUPS = fixtures_lookups("3375", doctored_spain_fixtures(), "2024")
((SPAIN_CLUBS_URL, _),) = SPAIN_LOOKUPS["clubs"]
((SPAIN_COMPETITIONS_URL, _),) = SPAIN_LOOKUPS["competitions"]


@pytest.mark.parametrize(
    ("overrides", "status", "url", "reason"),
    [
        pytest.param({"club": NOT_FOUND}, 404, club_url("3375"), "tmapi request failed (Club not found)", id="club"),
        pytest.param(
            {"fixtures": b"<html>"},
            502,
            f"{TMAPI_URL}/club/3375/fixtures?season=2024",
            "Unexpected tmapi response: body is not JSON",
            id="fixtures",
        ),
        pytest.param(
            {"clubs": [(SPAIN_CLUBS_URL, envelope({}))]},
            502,
            SPAIN_CLUBS_URL,
            "Unexpected tmapi response: lookup data is not a list of {id, name}",
            id="clubs",
        ),
        pytest.param(
            {"competitions": [(SPAIN_COMPETITIONS_URL, envelope({}))]},
            502,
            SPAIN_COMPETITIONS_URL,
            "Unexpected tmapi response: lookup data is not a list of {id, name}",
            id="competitions",
        ),
        pytest.param(
            {"attributes": b"<html>"},
            502,
            ATTRIBUTES_URL,
            "Unexpected tmapi response: body is not JSON",
            id="attributes",
        ),
        pytest.param(
            {"attributes": envelope({})},
            502,
            ATTRIBUTES_URL,
            "Unexpected tmapi response: attributes.competitionGroups is not a list of {id, name, ...}",
            id="attributes-groups",
        ),
        pytest.param(
            {"games": [(URL_GAME.format(game_id="4359336"), envelope({"actions": {}}))]},
            502,
            URL_GAME.format(game_id="4359336"),
            "Unexpected tmapi response: actions={} in game 4359336",
            id="game-report",
        ),
    ],
)
def test_fixtures_from_bytes_error_names_the_response(overrides, status, url, reason):
    kwargs = {
        "fixtures": doctored_spain_fixtures(),
        "club": fixture_bytes(club_url("3375")),
        **SPAIN_LOOKUPS,
        "attributes": fixture_bytes(ATTRIBUTES_URL),
        "games": list(SHOOTOUT_REPORTS.items()),
        **overrides,
    }
    with pytest.raises(UpstreamError) as e:
        TransfermarktClubFixtures.from_bytes(**kwargs, club_id="3375", season_id="2024")
    assert (e.value.status_code, e.value.url, e.value.reason) == (status, url, reason)


@pytest.mark.parametrize("service", [TransfermarktClubFixtures, TransfermarktClubSquad], ids=["fixtures", "squad"])
def test_fetch_unknown_club_body_is_404_with_the_club_url(service):
    client = DoctoredClient({club_url("131"): NOT_FOUND})
    with pytest.raises(UpstreamError) as e:
        asyncio.run(service.fetch(client, club_id="131", season_id="2024"))  # type: ignore[arg-type]
    assert (e.value.status_code, e.value.url) == (404, club_url("131"))
    assert e.value.reason == "tmapi request failed (Club not found)"


@pytest.mark.parametrize(
    ("service", "url"),
    [
        (TransfermarktClubFixtures, f"{TMAPI_URL}/club/131/fixtures?season=2024"),
        (TransfermarktClubSquad, f"{TMAPI_URL}/club/131/squad?season=2024"),
    ],
    ids=["fixtures", "squad"],
)
def test_fetch_malformed_list_is_502_with_the_season_url(service, url):
    client = DoctoredClient({url: b"<html>"})
    with pytest.raises(UpstreamError) as e:
        asyncio.run(service.fetch(client, club_id="131", season_id="2024"))  # type: ignore[arg-type]
    assert (e.value.status_code, e.value.url, e.value.reason) == (
        502,
        url,
        "Unexpected tmapi response: body is not JSON",
    )


def test_fixtures_requests():
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktClubFixtures.fetch(client, club_id="131", season_id="2024"))  # type: ignore[arg-type]
    games = tfmkt.games
    club_ids = {game[side]["clubId"] for game in games for side in ("homeClub", "awayClub")}
    assert client.urls == [
        club_url("131"),
        f"{TMAPI_URL}/club/131/fixtures?season=2024",
        *batch_urls("clubs", club_ids),
        *batch_urls("competitions", ["ES1", "CL", "CDR", "SUC"]),
    ]


def test_fixtures_unknown_club_is_404_before_other_requests():
    client = RecordingClient()
    with pytest.raises(UpstreamError) as e:
        asyncio.run(TransfermarktClubFixtures.fetch(client, club_id="0"))  # type: ignore[arg-type]
    assert e.value.status_code == 404
    assert client.urls == [club_url("0")]


def test_club_not_found_envelope_is_404():
    with pytest.raises(UpstreamError) as e:
        parse_club(club_url("0"), b'{"success":false,"message":"Club not found"}')
    assert e.value.status_code == 404
    assert "Club not found" in e.value.reason


def test_club_unexpected_shape_is_502():
    with pytest.raises(UpstreamError) as e:
        parse_club(club_url("131"), envelope({"name": "FC Barcelona", "baseDetails": {"isNationalTeam": 0}}))
    assert e.value.status_code == 502


# Squad


def squad_data(club_id: str, season_id: str | None = None) -> dict:
    return json.loads(fixture_bytes(TransfermarktClubSquad.squad_url(club_id, season_id)))["data"]


def player_lookups(player_ids: set[str]) -> list[tuple[str, bytes]]:
    return [(url, fixture_bytes(url)) for url in batch_urls("players", player_ids)]


def club_squad(
    club_id: str,
    season_id: str | None = None,
    squad: bytes | None = None,
    players: list[tuple[str, bytes]] | None = None,
    attributes: bytes | None = None,
) -> dict:
    squad = squad if squad is not None else fixture_bytes(TransfermarktClubSquad.squad_url(club_id, season_id))
    entries = TransfermarktClubSquad.parse_squad(squad, club_id=club_id, season_id=season_id)
    player_ids = {entry["playerId"] for entry in entries}
    tfmkt = TransfermarktClubSquad.from_bytes(
        squad,
        club=fixture_bytes(club_url(club_id)),
        players=players if players is not None else player_lookups(player_ids),
        attributes=attributes if attributes is not None else fixture_bytes(f"{TMAPI_URL}/attributes"),
        club_id=club_id,
        season_id=season_id,
    )
    return ClubSquadMembers.model_validate(tfmkt.get_club_squad()).model_dump(by_alias=True, mode="json")


def player_by_name(response: dict, name: str) -> dict:
    (player,) = [p for p in response["players"] if p["name"] == name]
    return player


def doctored_players(club_id: str, doctor, season_id: str | None = None) -> list[tuple[str, bytes]]:
    """The recorded /players lookups of a squad with `doctor` applied to their data lists."""
    player_ids = {entry["playerId"] for entry in squad_data(club_id, season_id)["squad"]}
    lookups = []
    for url, content in player_lookups(player_ids):
        data = json.loads(content)["data"]
        doctor(data)
        lookups.append((url, envelope(data)))
    return lookups


def raw_player(data: list, player_id: str) -> dict:
    return next(p for p in data if p["id"] == player_id)


def test_squad_club():
    response = club_squad("131")
    assert (response["id"], response["name"], response["isNationalTeam"], response["seasonId"]) == (
        "131",
        "FC Barcelona",
        False,
        None,
    )
    assert [p["id"] for p in response["players"]] == [e["playerId"] for e in squad_data("131")["squad"]]
    assert len(response["players"]) == 27
    assert {p["type"] for p in response["players"]} == {"current"}


def test_squad_national_team_and_season():
    response = club_squad("3375", "2024")
    assert (response["name"], response["isNationalTeam"], response["seasonId"]) == ("Spain", True, "2024")
    assert len(response["players"]) == 34
    assert {p["type"] for p in response["players"]} == {"historical"}
    assert {p["type"] for p in club_squad("3375")["players"]} == {"nationalTeam"}


def test_squad_player_one_nationality():
    assert player_by_name(club_squad("131"), "Lamine Yamal") == {
        "id": "937958",
        "name": "Lamine Yamal",
        "shirtNumber": 10,
        "isCaptain": False,
        "position": "Right Winger",
        "dateOfBirth": "2007-07-13",
        "age": 19,
        "nationalities": ["Spain"],
        "height": 183,
        "foot": "left",
        "contractUntil": "2031-06-30",
        "marketValue": 220000000,
        "type": "current",
    }


def test_squad_player_two_nationalities_and_captain():
    player = player_by_name(club_squad("131"), "Raphinha")
    assert player["nationalities"] == ["Brazil", "Italy"]
    assert player["isCaptain"] is True
    assert [p["name"] for p in club_squad("131")["players"] if p["isCaptain"]] == ["Raphinha"]


def test_squad_player_without_contract_and_market_value():
    # Xavi (retired): tmapi has no contract and a current market value of 0.
    player_ids = {entry["playerId"] for entry in squad_data("131", "2014")["squad"]}
    raw = raw_player([p for _, content in player_lookups(player_ids) for p in json.loads(content)["data"]], "7607")
    assert raw["attributes"]["contractUntil"] is None
    assert raw["marketValueDetails"]["current"]["value"] == 0
    player = player_by_name(club_squad("131", "2014"), "Xavi")
    assert (player["shirtNumber"], player["contractUntil"], player["marketValue"]) == (6, None, None)
    assert player["position"] == "Central Midfield"


def test_squad_player_without_shirt_number():
    assert any(e["shirtNumber"] is None for e in squad_data("3375")["squad"])
    assert player_by_name(club_squad("3375"), "Josep Martínez")["shirtNumber"] is None


def test_squad_player_missing_details():
    def doctor(data: list) -> None:
        player = raw_player(data, "937958")
        player["attributes"]["height"] = None
        del player["attributes"]["position"], player["attributes"]["preferredFoot"], player["marketValueDetails"]
        player["lifeDates"]["dateOfBirth"] = None
        player["lifeDates"]["age"] = None
        raw_player(data, "411295")["attributes"]["height"] = 0
        raw_player(data, "561613")["attributes"]["height"] = 2  # a whole number of metres is serialized as an int

    response = club_squad("131", players=doctored_players("131", doctor))
    yamal = player_by_name(response, "Lamine Yamal")
    assert {k: yamal[k] for k in ("height", "position", "foot", "marketValue", "dateOfBirth", "age")} == dict.fromkeys(
        ("height", "position", "foot", "marketValue", "dateOfBirth", "age")
    )
    assert player_by_name(response, "Raphinha")["height"] is None
    (player,) = [p for p in response["players"] if p["id"] == "561613"]
    assert player["height"] == 200


@pytest.mark.parametrize(
    ("doctor", "key"),
    [
        (lambda p: p["attributes"].update(position=None), "position"),
        (lambda p: p["attributes"].update(preferredFoot=None), "foot"),
        (lambda p: p.update(marketValueDetails=None), "marketValue"),
        (lambda p: p["marketValueDetails"].update(current=None), "marketValue"),
    ],
)
def test_squad_player_null_details_are_none(doctor, key):
    response = club_squad("131", players=doctored_players("131", lambda data: doctor(raw_player(data, "937958"))))
    yamal = player_by_name(response, "Lamine Yamal")
    assert yamal[key] is None
    assert {k: v for k, v in yamal.items() if k != key} == {
        k: v for k, v in player_by_name(club_squad("131"), "Lamine Yamal").items() if k != key
    }


def test_squad_country_missing_from_attributes_is_502():
    def doctor(data: list) -> None:
        raw_player(data, "411295")["nationalityDetails"]["nationalities"]["secondNationalityId"] = 99999

    with pytest.raises(UpstreamError) as e:
        club_squad("131", players=doctored_players("131", doctor))
    assert (e.value.status_code, e.value.url) == (502, ATTRIBUTES_URL)
    assert e.value.reason == "tmapi countries are missing id 99999 of player 411295"


def test_squad_player_without_first_nationality_keeps_the_second():
    def doctor(data: list) -> None:
        raw_player(data, "937958")["nationalityDetails"]["nationalities"].update(
            nationalityId=0, secondNationalityId=157
        )

    response = club_squad("131", players=doctored_players("131", doctor))
    assert player_by_name(response, "Lamine Yamal")["nationalities"] == ["Spain"]


def test_squad_player_missing_from_lookup_is_502():
    with pytest.raises(UpstreamError) as e:
        club_squad("131", players=doctored_players("131", lambda data: data.remove(raw_player(data, "937958"))))
    assert (e.value.status_code, e.value.url) == (502, f"{TMAPI_URL}/players")
    assert e.value.reason == "tmapi lookup is missing ids ['937958']"


@pytest.mark.parametrize(
    ("doctor", "message"),
    [
        (
            lambda d: raw_player(d, "937958")["lifeDates"].update(dateOfBirth="13/07/2007"),
            "lifeDates.dateOfBirth='13/07/2007' in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["attributes"].update(contractUntil="30/06/2031"),
            "attributes.contractUntil='30/06/2031' in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["attributes"].update(contractUntil=2031),
            "attributes.contractUntil=2031 in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["attributes"].update(height="1,83"),
            "attributes.height='1,83' in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["attributes"].update(position={}),
            "no attributes.position.name in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["attributes"].update(preferredFoot={}),
            "no attributes.preferredFoot.name in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["marketValueDetails"].update(current={}),
            "no marketValueDetails.current.value in player 937958",
        ),
        (
            lambda d: raw_player(d, "937958")["nationalityDetails"]["nationalities"].update(secondNationalityId=None),
            "nationalityDetails.nationalities.secondNationalityId=None in player 937958",
        ),
        (lambda d: d.append("937958"), "no id in player None"),
    ],
)
def test_squad_unexpected_player_is_502(doctor, message):
    lookups = doctored_players("131", doctor)
    with pytest.raises(UpstreamError) as e:
        club_squad("131", players=lookups)
    ((url, _),) = lookups
    assert (e.value.status_code, e.value.url) == (502, url)
    assert e.value.reason == f"Unexpected tmapi response: {message}"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (envelope({}), "Unexpected tmapi response: players data is not a list"),
        (b"<html>", "Unexpected tmapi response: body is not JSON"),
    ],
)
def test_squad_unexpected_players_lookup_is_502(content, reason):
    lookup_url = f"{TMAPI_URL}/players?ids[]=1"
    with pytest.raises(UpstreamError) as e:
        TransfermarktClubSquad.parse_players([(lookup_url, content)], player_ids=set())
    assert (e.value.status_code, e.value.url, e.value.reason) == (502, lookup_url, reason)


def test_squad_unexpected_shape_is_502():
    data = squad_data("131", "2014")
    data["squad"][0]["shirtNumber"] = "1"
    for content, reason in [
        (envelope(data), "Unexpected tmapi response: shirtNumber='1' in squad entry 74857"),
        (envelope({"squad": ["74857"]}), "Unexpected tmapi response: no playerId in squad entry None"),
        (envelope({"squad": {}}), "Unexpected tmapi response: no squad list"),
        (envelope([]), "Unexpected tmapi response: no squad list"),
        (b"<html>", "Unexpected tmapi response: body is not JSON"),
    ]:
        with pytest.raises(UpstreamError) as e:
            TransfermarktClubSquad.parse_squad(content, club_id="131", season_id="2014")
        assert (e.value.status_code, e.value.url, e.value.reason) == (
            502,
            f"{TMAPI_URL}/club/131/squad?season=2014",
            reason,
        )


COUNTRIES_REASON = "Unexpected tmapi response: attributes.countries is not a list of {id, name}"


@pytest.mark.parametrize(
    ("attributes", "reason"),
    [
        (envelope([]), COUNTRIES_REASON),
        (envelope({}), COUNTRIES_REASON),
        (envelope({"countries": ["Spain"]}), COUNTRIES_REASON),
        (envelope({"countries": [{"id": "157", "name": "Spain"}]}), COUNTRIES_REASON),
        (envelope({"countries": [{"id": 157}]}), COUNTRIES_REASON),
        (b"<html>", "Unexpected tmapi response: body is not JSON"),
    ],
)
def test_squad_unexpected_attributes_is_502(attributes, reason):
    with pytest.raises(UpstreamError) as e:
        club_squad("131", attributes=attributes)
    assert (e.value.status_code, e.value.url, e.value.reason) == (502, ATTRIBUTES_URL, reason)


@pytest.mark.parametrize(
    ("overrides", "status", "url", "reason"),
    [
        pytest.param({"club": NOT_FOUND}, 404, club_url("131"), "tmapi request failed (Club not found)", id="club"),
        pytest.param(
            {"squad": b"<html>"},
            502,
            f"{TMAPI_URL}/club/131/squad?season=2014",
            "Unexpected tmapi response: body is not JSON",
            id="squad",
        ),
    ],
)
def test_squad_from_bytes_error_names_the_response(overrides, status, url, reason):
    kwargs = {
        "squad": fixture_bytes(TransfermarktClubSquad.squad_url("131", "2014")),
        "club": fixture_bytes(club_url("131")),
    }
    with pytest.raises(UpstreamError) as e:
        TransfermarktClubSquad.from_bytes(**{**kwargs, **overrides}, players=[], club_id="131", season_id="2014")
    assert (e.value.status_code, e.value.url, e.value.reason) == (status, url, reason)


def test_squad_empty_from_bytes_needs_no_attributes():
    tfmkt = TransfermarktClubSquad.from_bytes(
        envelope({"squad": []}), club=fixture_bytes(club_url("131")), players=[], club_id="131", season_id="1800"
    )
    assert (tfmkt.countries, tfmkt.get_club_squad()["players"]) == ({}, [])


def test_squad_empty():
    assert squad_data("131", "1800") == {"clubId": "131", "squad": [], "playerIds": []}
    client = RecordingClient()
    tfmkt = asyncio.run(TransfermarktClubSquad.fetch(client, club_id="131", season_id="1800"))  # type: ignore[arg-type]
    assert tfmkt.get_club_squad()["players"] == []
    assert client.urls == [club_url("131"), f"{TMAPI_URL}/club/131/squad?season=1800"]


def test_squad_requests():
    client = RecordingClient()
    asyncio.run(TransfermarktClubSquad.fetch(client, club_id="131"))  # type: ignore[arg-type]
    player_ids = {entry["playerId"] for entry in squad_data("131")["squad"]}
    assert client.urls == [
        club_url("131"),
        f"{TMAPI_URL}/club/131/squad",
        *batch_urls("players", player_ids),
        f"{TMAPI_URL}/attributes",
    ]


def test_squad_unknown_club_is_404_before_other_requests():
    client = RecordingClient()
    with pytest.raises(UpstreamError) as e:
        asyncio.run(TransfermarktClubSquad.fetch(client, club_id="0"))  # type: ignore[arg-type]
    assert e.value.status_code == 404
    assert client.urls == [club_url("0")]
