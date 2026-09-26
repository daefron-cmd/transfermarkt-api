"""Parser tests that run a service's from_bytes on a recorded fixture page."""

import json
from datetime import date

from app.schemas.clubs.profile import ClubProfile
from app.schemas.players.injuries import PlayerInjuries
from app.services.clubs.profile import TransfermarktClubProfile
from app.services.players.injuries import TransfermarktPlayerInjuries
from app.services.players.profile import TransfermarktPlayerProfile
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
