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
    "stats_937958": "/players/937958/stats",
    "stats_937958_2024": "/players/937958/stats?season_id=2024",
    "stats_912902": "/players/912902/stats",
    "stats_0": "/players/0/stats",
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
    "club_players_0": "/clubs/0/players",
    "club_players_3375": "/clubs/3375/players",
    "club_players_3375_2024": "/clubs/3375/players?season_id=2024",
    "club_fixtures_131": "/clubs/131/fixtures",
    "club_fixtures_131_2024": "/clubs/131/fixtures?season_id=2024",
    "club_fixtures_3375": "/clubs/3375/fixtures",
    "club_fixtures_3375_2024": "/clubs/3375/fixtures?season_id=2024",
    "club_fixtures_0": "/clubs/0/fixtures",
    "club_squad_131": "/clubs/131/squad",
    "club_squad_131_2014": "/clubs/131/squad?season_id=2014",
    "club_squad_131_1800": "/clubs/131/squad?season_id=1800",
    "club_squad_3375": "/clubs/3375/squad",
    "club_squad_3375_2024": "/clubs/3375/squad?season_id=2024",
    "club_squad_0": "/clubs/0/squad",
    # Competitions
    "comp_search_premier": "/competitions/search/premier",
    "comp_search_euro": "/competitions/search/euro",
    "comp_search_world_cup": "/competitions/search/world%20cup",
    "comp_search_nations": "/competitions/search/nations",
    "comp_clubs_ES1": "/competitions/ES1/clubs",
    "comp_clubs_ES1_2023": "/competitions/ES1/clubs?season_id=2023",
    "comp_clubs_GB1": "/competitions/GB1/clubs",
    "comp_table_ES1": "/competitions/ES1/table",
    "comp_table_ES1_2024": "/competitions/ES1/table?season_id=2024",
    "comp_table_CL": "/competitions/CL/table",
    "comp_table_CLI": "/competitions/CLI/table",
    "comp_table_UNLA": "/competitions/UNLA/table",
    "comp_table_CDR": "/competitions/CDR/table",
    "comp_table_XX": "/competitions/XX/table",
    "comp_fixtures_ES1_2024": "/competitions/ES1/fixtures?season_id=2024",
    "comp_fixtures_ES1_2024_md1": "/competitions/ES1/fixtures?season_id=2024&matchday=1",
    "comp_fixtures_ES1_2024_md99": "/competitions/ES1/fixtures?season_id=2024&matchday=99",
    "comp_fixtures_EURO_2023": "/competitions/EURO/fixtures?season_id=2023",
    "comp_fixtures_CDR": "/competitions/CDR/fixtures",
    "comp_fixtures_XX": "/competitions/XX/fixtures",
}
