"""
Live smoke tests: detect Transfermarkt HTML/JSON drift that the recorded fixtures cannot show.

Only stable facts (ids, names, birth dates, past transfers and squads, taken from tests/snapshots) and structural
invariants are asserted; volatile values (market values, ages, current club, injuries, current-season counts) are not.
Run with: uv run pytest tests/live -m live --force-enable-socket -q
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

pytestmark = pytest.mark.live

SEASON_ID = re.compile(r"^\d{4}$")


def assert_iso_date(value: Any) -> None:
    assert isinstance(value, str), value
    assert date.fromisoformat(value).isoformat() == value, value


def assert_numeric_id(value: Any) -> None:
    assert isinstance(value, str), value
    assert value.isdigit(), value


def check_player_search(body: dict) -> None:
    results = body["results"]
    assert any(r["id"] == "28003" and "Messi" in r["name"] for r in results)
    for result in results:
        assert_numeric_id(result["id"])


def check_player_profile(body: dict) -> None:
    assert body["name"] == "Lionel Messi"
    assert body["dateOfBirth"] == "1987-06-24"
    assert body["citizenship"]
    assert_numeric_id(body["club"]["id"])


def check_market_value(body: dict) -> None:
    history = body["marketValueHistory"]
    assert history
    for entry in history:
        assert_iso_date(entry["date"])
        assert isinstance(entry["marketValue"], int), entry
    assert any(entry["clubId"] for entry in history)


def check_transfers(body: dict) -> None:
    transfers = body["transfers"]
    assert transfers
    psg = [t for t in transfers if t["clubFrom"]["id"] == "131" and t["clubTo"]["id"] == "583"]
    assert len(psg) == 1
    assert_iso_date(psg[0]["date"])
    assert psg[0]["date"].startswith("2021-")
    for transfer in transfers:
        assert transfer.get("fee") is None or isinstance(transfer["fee"], int), transfer


def check_jersey_numbers(body: dict) -> None:
    numbers = body["jerseyNumbers"]
    assert numbers
    for entry in numbers:
        assert entry["season"]
        assert isinstance(entry["jerseyNumber"], int), entry


def check_stats(body: dict) -> None:
    stats = body["stats"]
    assert stats
    for row in stats:
        assert SEASON_ID.match(row["seasonId"]), row
        assert row["competitionName"], row
        assert row["clubName"], row
        # A (season, competition, club) where the player was only in the squad is returned with 0 appearances.
        assert isinstance(row["appearances"], int), row
        assert row["appearances"] >= 0, row
    assert any(row["appearances"] >= 1 for row in stats)
    assert sum(row["appearances"] for row in stats) > 50


def check_stats_season(body: dict) -> None:
    assert body["stats"]
    assert {row["seasonId"] for row in body["stats"]} == {"2024"}


def check_goalkeeper_stats(body: dict) -> None:
    assert any(row["cleanSheets"] is not None and row["goalsConceded"] is not None for row in body["stats"])


def check_injuries(body: dict) -> None:
    injuries = body["injuries"]
    assert injuries
    for injury in injuries:
        assert_iso_date(injury["fromDate"])
        assert isinstance(injury["days"], int), injury
        # gamesMissed is null (omitted) when no games were missed.
        assert injury.get("gamesMissed") is None or isinstance(injury["gamesMissed"], int), injury
    assert any(isinstance(injury.get("gamesMissed"), int) for injury in injuries)


def check_achievements(body: dict) -> None:
    achievements = body["achievements"]
    assert achievements
    for achievement in achievements:
        assert achievement["title"]
        assert achievement["details"]


def check_club_search(body: dict) -> None:
    assert any(r["id"] == "131" for r in body["results"])


def check_club_profile(body: dict) -> None:
    assert body["name"] == "FC Barcelona"
    assert body["league"]["id"] == "ES1"
    assert body["stadiumName"]
    assert_iso_date(body["foundedOn"])
    assert body.get("fifaWorldRanking") is None or isinstance(body["fifaWorldRanking"], int)
    assert isinstance(body["squad"]["size"], int)
    assert body["squad"]["size"] > 15


def check_club_players(body: dict) -> None:
    assert SEASON_ID.match(body["seasonId"])
    players = body["players"]
    assert 15 < len(players) < 60
    for player in players:
        assert_numeric_id(player["id"])
        assert player["name"]
        assert_iso_date(player["dateOfBirth"])
        assert isinstance(player["age"], int), player
        assert player["nationality"], player
    # signedFromFee is parsed from the transfer link's title; a label change shows up as no fees at all.
    assert any(isinstance(player.get("signedFromFee"), int) for player in players)
    assert any(player.get("signedFrom") for player in players)
    assert sum(isinstance(player.get("height"), int) for player in players) >= len(players) / 2


def check_club_players_2014(body: dict) -> None:
    assert body["seasonId"] == "2014"
    players = {player["id"]: player for player in body["players"]}
    xavi = players["7607"]
    assert xavi["name"] == "Xavi"
    assert xavi["signedFrom"] == "FC Barcelona B"
    assert xavi["joinedOn"] == "1998-07-01"
    suarez = players["44352"]
    assert suarez["name"] == "Luis Suárez"
    assert suarez["signedFromFee"] == 81720000


def check_competition_search(body: dict) -> None:
    assert any(r["id"] == "GB1" for r in body["results"])


def check_competition_clubs(body: dict) -> None:
    assert SEASON_ID.match(body["seasonId"])
    assert any(club["id"] == "131" for club in body["clubs"])


def check_competition_clubs_2023(body: dict) -> None:
    assert body["seasonId"] == "2023"
    assert any(club["id"] == "131" for club in body["clubs"])


def check_competition_table_2024(body: dict) -> None:
    (table,) = body["tables"]
    assert len(table["rows"]) == 20
    first = table["rows"][0]
    assert (first["position"], first["clubId"], first["points"]) == (1, "131", 88)


def check_competition_fixtures_euro(body: dict) -> None:
    games = body["games"]
    assert len(games) == 51
    (final,) = [game for game in games if game["stage"] == "Final"]
    assert (final["homeClub"]["id"], final["awayClub"]["id"]) == ("3375", "3299")
    assert (final["homeGoals"], final["awayGoals"]) == (2, 1)
    assert sum(game["shootout"] is not None for game in games) == 3


def check_club_fixtures_2024(body: dict) -> None:
    assert body["seasonId"] == "2024"
    games = body["games"]
    assert len(games) == 60
    assert all(game["venue"] in {"home", "away"} for game in games)
    (final,) = [game for game in games if game["competitionId"] == "CDR" and game["stage"] == "Final"]
    assert final["result"] == "W"


def check_club_squad(body: dict) -> None:
    players = body["players"]
    assert 15 < len(players) < 60
    for player in players:
        assert_numeric_id(player["id"])
        assert player["name"], player
        assert player["type"] == "current", player
        assert player["nationalities"], player
    assert any(player["isCaptain"] for player in players)


def check_national_squad(body: dict) -> None:
    assert body["isNationalTeam"] is True
    players = body["players"]
    assert 15 < len(players) < 60
    assert all(player["type"] == "nationalTeam" for player in players)


def check_national_players(body: dict) -> None:
    assert SEASON_ID.match(body["seasonId"])
    players = body["players"]
    assert 15 < len(players) < 60
    for player in players:
        assert_iso_date(player["dateOfBirth"])
        assert isinstance(player["internationalMatches"], int), player


def check_competition_search_euro(body: dict) -> None:
    assert any(r["id"] == "EURO" for r in body["results"])


CASES: dict[str, tuple[str, Callable[[dict], None]]] = {
    "player_search": ("/players/search/messi", check_player_search),
    "player_profile": ("/players/28003/profile", check_player_profile),
    "market_value": ("/players/28003/market_value", check_market_value),
    "transfers": ("/players/28003/transfers", check_transfers),
    "jersey_numbers": ("/players/28003/jersey_numbers", check_jersey_numbers),
    "stats": ("/players/937958/stats", check_stats),
    "stats_season": ("/players/937958/stats?season_id=2024", check_stats_season),
    "stats_goalkeeper": ("/players/912902/stats", check_goalkeeper_stats),
    "injuries": ("/players/28003/injuries", check_injuries),
    "achievements": ("/players/28003/achievements", check_achievements),
    "club_search": ("/clubs/search/barcelona", check_club_search),
    "club_profile": ("/clubs/131/profile", check_club_profile),
    "club_players": ("/clubs/131/players", check_club_players),
    "club_players_2014": ("/clubs/131/players?season_id=2014", check_club_players_2014),
    "competition_search": ("/competitions/search/premier", check_competition_search),
    "competition_clubs": ("/competitions/ES1/clubs", check_competition_clubs),
    "competition_clubs_2023": ("/competitions/ES1/clubs?season_id=2023", check_competition_clubs_2023),
    "competition_table_2024": ("/competitions/ES1/table?season_id=2024", check_competition_table_2024),
    "competition_fixtures_euro": ("/competitions/EURO/fixtures?season_id=2023", check_competition_fixtures_euro),
    "club_fixtures_2024": ("/clubs/131/fixtures?season_id=2024", check_club_fixtures_2024),
    "club_squad": ("/clubs/131/squad", check_club_squad),
    "national_squad": ("/clubs/3375/squad", check_national_squad),
    "national_players": ("/clubs/3375/players", check_national_players),
    "competition_search_euro": ("/competitions/search/euro", check_competition_search_euro),
}


@pytest.mark.parametrize("path,check", CASES.values(), ids=CASES.keys())
def test_endpoint(client, path: str, check: Callable[[dict], None]):
    response = client.get(path)

    assert response.status_code == 200, response.text
    check(response.json())
