"""Endpoint cases shared by the fixture recorder and the offline snapshot tests."""

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
FIXTURES_INDEX = FIXTURES_DIR / "index.json"
SNAPSHOTS_DIR = TESTS_DIR / "snapshots"

RECORD_COMMAND = "uv run python scripts/record_fixtures.py"

CASES: dict[str, str] = {
    # Players
    "search_messi_p1": "/players/search/messi",
    "search_messi_p2": "/players/search/messi?page_number=2",
    "profile_28003": "/players/28003/profile",
    "profile_8198": "/players/8198/profile",
    "profile_68290": "/players/68290/profile",
    "profile_3373": "/players/3373/profile",
    "profile_0": "/players/0/profile",
    "market_value_28003": "/players/28003/market_value",
    "market_value_3373": "/players/3373/market_value",
    "transfers_28003": "/players/28003/transfers",
    "transfers_3373": "/players/3373/transfers",
    "jersey_numbers_28003": "/players/28003/jersey_numbers",
    "stats_8198": "/players/8198/stats",
    "stats_5023": "/players/5023/stats",
    "injuries_28003_p1": "/players/28003/injuries",
    "injuries_8198_p1": "/players/8198/injuries",
    "achievements_28003": "/players/28003/achievements",
    "achievements_3373": "/players/3373/achievements",
    # Clubs
    "club_search_barcelona": "/clubs/search/barcelona",
    "club_search_ab": "/clubs/search/a%26b",
    "club_profile_131": "/clubs/131/profile",
    "club_profile_27": "/clubs/27/profile",
    "club_profile_3437": "/clubs/3437/profile",
    "club_profile_0": "/clubs/0/profile",
    "club_players_131_current": "/clubs/131/players",
    "club_players_131_2014": "/clubs/131/players?season_id=2014",
    "club_players_210_2017": "/clubs/210/players?season_id=2017",
    # Competitions
    "comp_search_premier": "/competitions/search/premier",
    "comp_clubs_ES1": "/competitions/ES1/clubs",
    "comp_clubs_ES1_2023": "/competitions/ES1/clubs?season_id=2023",
    "comp_clubs_GB1": "/competitions/GB1/clubs",
}
