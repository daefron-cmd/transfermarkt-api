"""Parser tests that run a service's from_bytes on a recorded fixture page."""

import json
from datetime import date

import pytest
from fastapi import HTTPException

from app.http import UpstreamError
from app.schemas.clubs.profile import ClubProfile
from app.schemas.players.injuries import PlayerInjuries
from app.schemas.players.stats import PlayerStats
from app.services.clubs.profile import TransfermarktClubProfile
from app.services.players.injuries import TransfermarktPlayerInjuries
from app.services.players.profile import TransfermarktPlayerProfile
from app.services.players.stats import TransfermarktPlayerStats
from app.tmapi import TMAPI_URL, batch_urls, names_by_id, tmapi_data
from tests.endpoints.cases import FIXTURES_DIR, FIXTURES_INDEX


def fixture_bytes(url: str) -> bytes:
    entry = next(e for e in json.loads(FIXTURES_INDEX.read_text()) if e["url"] == url)
    return (FIXTURES_DIR / entry["file"]).read_bytes()


def club_profile(club_id: str) -> dict:
    html = fixture_bytes(f"https://www.transfermarkt.us/-/datenfakten/verein/{club_id}")
    return TransfermarktClubProfile.from_bytes(html, club_id=club_id).get_club_profile()


def test_club_profile_members_and_date():
    profile = ClubProfile.model_validate(club_profile("131"))
    assert profile.members == 170_000
    assert profile.members_date == date(2022, 1, 1)
    assert profile.stadium_seats == 62_657


def test_club_profile_league_country_id():
    assert club_profile("131")["league"]["countryId"] == "157"
    assert club_profile("27")["league"]["countryId"] == "40"


def test_club_profile_national_team():
    profile = ClubProfile.model_validate(club_profile("3437"))
    assert profile.fifa_world_ranking == 2
    assert profile.stadium_name is None
    assert profile.stadium_seats is None
    assert profile.current_transfer_record is None
    assert profile.squad.national_team_players is None


def test_club_profile_no_fifa_ranking_for_clubs():
    assert club_profile("131")["fifaWorldRanking"] is None


def test_player_profile_date_of_birth_and_age():
    html = fixture_bytes("https://www.transfermarkt.com/-/profil/spieler/28003")
    profile = TransfermarktPlayerProfile.from_bytes(html, player_id="28003").get_player_profile()
    assert profile["dateOfBirth"] == "24/06/1987"
    assert profile["age"] == "39"


def test_player_injuries_games_missed():
    html = fixture_bytes("https://www.transfermarkt.com/player/verletzungen/spieler/28003/plus/1/page/1")
    injuries = TransfermarktPlayerInjuries.from_bytes(html, player_id="28003", page_number=1).get_player_injuries()
    rows = PlayerInjuries.model_validate(injuries).injuries
    # First row has "-" in the games-missed cell and no club links.
    assert rows[0].games_missed is None
    assert rows[0].games_missed_clubs == []
    assert rows[0].days == 12
    assert rows[5].games_missed == 10
    assert rows[5].games_missed_clubs == ["69261", "3437"]


def test_club_profile_confederation():
    assert club_profile("3437")["confederation"] == "South American Football Confederation"
    # Club pages have no confederation entry in the header.
    assert club_profile("131")["confederation"] is None


def player_stats(player_id: str, season_id: str | None = None) -> PlayerStats:
    performance = fixture_bytes(TransfermarktPlayerStats.URL_TEMPLATE.format(player_id=player_id))
    rows = TransfermarktPlayerStats.parse_performance(performance, player_id=player_id, season_id=season_id)

    def lookups(resource: str, ids: set[str]) -> list[tuple[str, bytes]]:
        return [(url, fixture_bytes(url)) for url in batch_urls(resource, ids)]

    tfmkt = TransfermarktPlayerStats.from_bytes(
        performance,
        player=fixture_bytes(TransfermarktPlayerStats.URL_PLAYER.format(player_id=player_id)),
        attributes=fixture_bytes(TransfermarktPlayerStats.URL_ATTRIBUTES),
        competitions=lookups("competitions", TransfermarktPlayerStats.competition_ids(rows)),
        clubs=lookups("clubs", TransfermarktPlayerStats.club_ids(rows)),
        player_id=player_id,
        season_id=season_id,
    )
    return PlayerStats.model_validate(tfmkt.get_player_stats())


def stat_row(stats: PlayerStats, season_id: str, competition_id: str, club_id: str):
    (row,) = [
        s for s in stats.stats if (s.season_id, s.competition_id, s.club_id) == (season_id, competition_id, club_id)
    ]
    return row


def test_player_stats_outfield_aggregates():
    # Counted by hand from the 38 raw rows of Yamal's 2024 LaLiga games for Barcelona (35 played, 3 injured).
    row = stat_row(player_stats("937958"), "2024", "ES1", "131")
    assert (row.season_name, row.competition_name, row.club_name) == ("24/25", "LaLiga", "FC Barcelona")
    assert row.appearances == 35
    assert row.goals == 9
    assert row.assists == 15
    assert row.minutes_played == 2864
    assert (row.yellow_cards, row.second_yellow_cards, row.red_cards) == (3, 0, 0)
    assert row.goals_conceded is None
    assert row.clean_sheets is None


def test_player_stats_outfield_player_games_without_position_get_no_goalkeeper_stats():
    # Yamal (main position Right Winger) has played games with positionId 0.
    stats = player_stats("937958").stats
    assert all(s.goals_conceded is None and s.clean_sheets is None for s in stats)


def test_player_stats_national_team_games_use_the_fielded_club():
    stats = player_stats("937958")
    assert {s.club_name for s in stats.stats if s.competition_id == "EURO"} == {"Spain"}


# External ground truth: Restes' rows in Transfermarkt's own rendered detailed-stats table (checked by the
# supervisor on the live site), as (season, competition, club): (appearances, goals conceded, clean sheets).
# The youth/reserve rows (FR5L, F19D, FRYC) are games with positionId 0, counted through the main position.
RESTES_SITE_ROWS = {
    ("2026", "FR1", "415"): (4, 6, 0),
    ("2025", "FR1", "415"): (34, 45, 10),
    ("2024", "FR1", "415"): (29, 36, 9),
    ("2023", "FR1", "415"): (34, 46, 6),
    ("2023", "EL", "415"): (8, 11, 3),
    ("2023", "FRCH", "415"): (1, 2, 0),
    ("2024", "FRC", "415"): (2, 3, 0),
    ("2022", "FR5L", "9371"): (None, 9, 3),
    ("2021", "F19D", "9372"): (None, 4, 5),
    ("2021", "FRYC", "9372"): (None, 1, 0),
    ("2021", "FR5L", "9371"): (None, 4, 2),
    ("2022", "F19D", "9372"): (None, 3, 0),
}


@pytest.mark.parametrize(("key", "expected"), list(RESTES_SITE_ROWS.items()))
def test_player_stats_goalkeeper_matches_site(key, expected):
    row = stat_row(player_stats("912902"), *key)
    appearances, goals_conceded, clean_sheets = expected
    if appearances is not None:
        assert row.appearances == appearances
    assert (row.goals_conceded, row.clean_sheets) == (goals_conceded, clean_sheets)


def test_player_stats_goalkeeper_aggregates():
    # Counted by hand from Restes' 29 played 2024 Ligue 1 games for Toulouse (positionId 1, Goalkeeper).
    row = stat_row(player_stats("912902"), "2024", "FR1", "415")
    assert row.minutes_played == 2545
    assert (row.yellow_cards, row.red_cards) == (2, 0)


def test_player_stats_red_cards():
    # Two of Restes' 12 played 2022 games for Toulouse B have a cardStatistics.redCard entry.
    row = stat_row(player_stats("912902"), "2022", "FR5L", "9371")
    assert row.appearances == 12
    assert row.minutes_played == 919
    assert (row.yellow_cards, row.second_yellow_cards, row.red_cards) == (0, 0, 2)


def test_player_stats_season_filter_and_order():
    stats = player_stats("937958", season_id="2024").stats
    assert {s.season_id for s in stats} == {"2024"}
    assert [s.competition_name for s in stats] == sorted(s.competition_name for s in stats)
    all_seasons = [int(s.season_id) for s in player_stats("937958").stats]
    assert all_seasons == sorted(all_seasons, reverse=True)


@pytest.mark.parametrize(
    "body",
    [
        b'{"success": false, "message": "Playerperformancegame not found"}',
        b'{"success": true, "data": {"performance": []}}',
    ],
)
def test_player_stats_unknown_player_is_404(body):
    with pytest.raises(HTTPException) as e:
        TransfermarktPlayerStats.parse_performance(body, player_id="0")
    assert e.value.status_code == 404


def test_player_stats_rejects_changed_row_shape():
    body = json.loads(fixture_bytes(TransfermarktPlayerStats.URL_TEMPLATE.format(player_id="937958")))
    del body["data"]["performance"][0]["statistics"]["goalStatistics"]["goalsScoredTotal"]
    with pytest.raises(UpstreamError, match="goalsScoredTotal"):
        TransfermarktPlayerStats.parse_performance(json.dumps(body).encode(), player_id="937958")


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"success": true}'])
def test_tmapi_data_rejects_unexpected_envelopes(body):
    with pytest.raises(UpstreamError) as e:
        tmapi_data("https://tmapi.example/x", body)
    assert e.value.status_code == 502


def test_batch_urls_are_sorted_deduplicated_and_chunked():
    ids = [str(i) for i in range(120, 0, -1)] + ["7"]
    urls = batch_urls("clubs", ids)
    assert len(urls) == 3
    assert urls[0].startswith(f"{TMAPI_URL}/clubs?ids[]=1&ids[]=10&ids[]=100&")
    assert [url.count("ids[]=") for url in urls] == [50, 50, 20]


def test_names_by_id_strips_whitespace():
    body = b'{"success": true, "data": [{"id": "ES2P", "name": "Promoci\\u00f3n de ascenso a LaLiga2 "}]}'
    assert names_by_id("https://tmapi.example/competitions", body) == {"ES2P": "Promoción de ascenso a LaLiga2"}
