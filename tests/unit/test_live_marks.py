from tests.live.test_smoke import CASES, PARAMS


def test_tmapi_marked_cases():
    marked = {param.id for param in PARAMS if any(mark.name == "tmapi" for mark in param.marks)}

    assert marked == {
        "stats",
        "stats_season",
        "stats_goalkeeper",
        "competition_table_2024",
        "competition_fixtures_euro",
        "club_fixtures_2024",
        "club_squad",
        "national_squad",
        "game_report",
    }
    assert [param.id for param in PARAMS] == list(CASES)
