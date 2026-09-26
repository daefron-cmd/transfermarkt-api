"""Parser tests that run a service's from_bytes on a recorded fixture page."""

import json
from datetime import date

import lxml.html
import pytest
from fastapi import HTTPException

from app.http import UpstreamError
from app.schemas.clubs.players import ClubPlayers
from app.schemas.clubs.profile import ClubProfile
from app.schemas.competitions.search import CompetitionSearch
from app.schemas.players.injuries import PlayerInjuries
from app.schemas.players.stats import PlayerStats
from app.services.clubs.players import TransfermarktClubPlayers
from app.services.clubs.profile import TransfermarktClubProfile
from app.services.competitions.search import TransfermarktCompetitionSearch
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
    assert e.value.detail == f"Invalid request (url: {TMAPI_URL}/player/0/performance-game)"


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


@pytest.mark.parametrize(
    "club_id,season_id,url",
    [
        ("131", None, "https://www.transfermarkt.com/-/kader/verein/131/plus/1"),
        ("210", "2017", "https://www.transfermarkt.com/-/kader/verein/210/saison_id/2017/plus/1"),
    ],
)
def test_club_players_blank_strings_are_omitted(club_id, season_id, url):
    service = TransfermarktClubPlayers.from_bytes(fixture_bytes(url), club_id=club_id, season_id=season_id)
    raw = service.get_club_players()
    players = ClubPlayers.model_validate(raw).model_dump(mode="json", by_alias=True, exclude_none=True)["players"]

    no_status = [i for i, p in enumerate(raw["players"]) if not p["status"].strip()]
    assert no_status
    for i in no_status:
        assert "status" not in players[i]
    for player in players:
        for key in ("joinedOn", "signedFrom", "status"):
            assert player.get(key) != "", (player["id"], key)
    assert any("status" in p for p in players)


CLUB_PLAYERS_URL = TransfermarktClubPlayers.URL_TEMPLATE


def club_players(
    club_id: str, fixture_season: str | None, *, season_id: str | None = None, html: bytes | None = None
) -> dict:
    """Parse the recorded squad page of fixture_season (or the given html), passing season_id to the service."""
    if html is None:
        season = f"/saison_id/{fixture_season}" if fixture_season else ""
        html = fixture_bytes(CLUB_PLAYERS_URL.format(club_id=club_id, season=season))
    return TransfermarktClubPlayers.from_bytes(html, club_id=club_id, season_id=season_id).get_club_players()


def players_by_name(raw: dict) -> dict:
    return {p.name: p for p in ClubPlayers.model_validate(raw).players}


def test_club_players_signed_from_and_fee():
    # Mascherano, Xavi and Suárez have a fee label (": Ablöse …") as crest title; Rakitic's crest title is the club.
    players = players_by_name(club_players("131", "2014", season_id="2014"))
    signed = {name: (players[name].signed_from, players[name].signed_from_fee) for name in players}
    assert signed["Javier Mascherano"] == ("Liverpool FC", 20_000_000)
    assert signed["Xavi"] == ("FC Barcelona B", None)
    assert signed["Luis Suárez"] == ("Liverpool FC", 81_720_000)
    assert signed["Ivan Rakitic"] == ("Sevilla FC", 18_000_000)
    assert signed["Adama Traoré"] == ("Wolverhampton Wanderers", None)  # "Ablöse ?"
    assert signed["Edgar Ié"] == (None, None)  # empty cell


def test_club_players_signed_from_falls_back_to_link_title():
    url = CLUB_PLAYERS_URL.format(club_id="131", season="/saison_id/2014")
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (img,) = page.xpath("//a[@title='Liverpool FC: Ablöse €20.00m']/img")
    del img.attrib["alt"]
    players = players_by_name(club_players("131", "2014", html=lxml.html.tostring(page), season_id="2014"))
    assert players["Javier Mascherano"].signed_from == "Liverpool FC"
    assert players["Javier Mascherano"].signed_from_fee == 20_000_000


@pytest.mark.parametrize("label", ["Fee", "Transfer fee"])
def test_club_players_signed_from_fee_ignores_the_label(label):
    url = CLUB_PLAYERS_URL.format(club_id="131", season="/saison_id/2014")
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (link,) = page.xpath("//a[@title='Liverpool FC: Ablöse €20.00m']")
    link.set("title", f"Liverpool FC: {label} €20.00m")
    players = players_by_name(club_players("131", "2014", html=lxml.html.tostring(page), season_id="2014"))
    assert players["Javier Mascherano"].signed_from == "Liverpool FC"
    assert players["Javier Mascherano"].signed_from_fee == 20_000_000


def test_club_players_current_season_signed_from_fee():
    players = players_by_name(club_players("131", None))
    assert (players["Frenkie de Jong"].signed_from, players["Frenkie de Jong"].signed_from_fee) == (
        "Ajax Amsterdam",
        86_000_000,
    )
    assert players["Wojciech Szczesny"].signed_from == "Career break"


def test_club_players_have_no_joined_field():
    for club_id, season_id in [("131", None), ("131", "2014"), ("210", "2017")]:
        raw = club_players(club_id, season_id, season_id=season_id)
        assert all("joined" not in p for p in raw["players"])


@pytest.mark.parametrize(
    ("season_id", "fixture_season", "expected"),
    [(None, None, "2026"), (None, "2014", "2014"), ("2014", "2014", "2014")],
)
def test_club_players_season_id(season_id, fixture_season, expected):
    raw = club_players("131", fixture_season, season_id=season_id)
    assert ClubPlayers.model_validate(raw).season_id == expected
    assert raw["seasonId"] == expected


@pytest.mark.parametrize(
    ("season", "cell", "field"),
    [
        (None, "td[@class='rechts hauptlink']", "marketValue"),
        ("2014", "td[@class='zentriert'][img[@class='flaggenrahmen']]", "nationality"),
    ],
)
def test_club_players_misaligned_columns_raise(season, cell, field):
    url = CLUB_PLAYERS_URL.format(club_id="131", season=f"/saison_id/{season}" if season else "")
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (td,) = page.xpath(f"(//div[@id='yw1']//table[@class='items']/tbody/tr)[3]/{cell}")
    td.getparent().remove(td)
    with pytest.raises(UpstreamError, match=field) as e:
        club_players("131", season, html=lxml.html.tostring(page), season_id=season)
    assert (e.value.status_code, e.value.url) == (502, url)


def test_club_players_height_na_link_keeps_rows_aligned():
    # Cícero, Maicon and Maicosuel have "<a>N/A</a>" as height; the players after them keep their own heights.
    players = players_by_name(club_players("210", "2017", season_id="2017"))
    assert len(players) == 66
    heights = [players[name].height for name in ("Cícero", "Maicon", "Luan", "Maicosuel", "Jael")]
    assert heights == [None, None, 180, None, 186]


def test_club_players_national_team():
    raw = club_players("3375", None)
    assert raw["seasonId"] == "2026"
    players = ClubPlayers.model_validate(raw).model_dump(mode="json", by_alias=True, exclude_none=True)["players"]
    assert len(players) == 26
    assert players[0] == {
        "id": "262749",
        "name": "David Raya",
        "position": "Goalkeeper",
        "dateOfBirth": "1995-09-15",
        "age": 31,
        "currentClub": "Arsenal FC",
        "height": 186,
        "foot": "right",
        "internationalMatches": 13,
        "internationalGoals": 0,
        "debut": "2022-03-26",
        "marketValue": 30_000_000,
    }
    by_name = {p["name"]: p for p in players}
    # Uncapped: "-" in both columns and an empty debut cell.
    fresneda = by_name["Iván Fresneda"]
    assert (fresneda["internationalMatches"], fresneda["internationalGoals"], "debut" in fresneda) == (0, 0, False)
    assert (by_name["Mikel Oyarzabal"]["internationalMatches"], by_name["Mikel Oyarzabal"]["internationalGoals"]) == (
        61,
        30,
    )


def test_club_players_national_team_past_season():
    raw = club_players("3375", "2024", season_id="2024")
    players = {p.name: p for p in ClubPlayers.model_validate(raw).players}
    assert len(players) == 46
    assert players["Jesús Navas"].current_club == "Retired"
    assert players["Jesús Navas"].market_value is None
    assert players["Daniel Carvajal"].current_club == "Without Club"
    assert players["Álvaro Morata"].international_matches == 87
    assert players["Álvaro Morata"].debut == date(2014, 11, 15)


def test_club_players_unknown_layout_raises():
    url = CLUB_PLAYERS_URL.format(club_id="3375", season="")
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (link,) = page.xpath("//div[@id='yw1']//thead//th/a[text()='Debut']")
    link.text = "First game"
    with pytest.raises(UpstreamError, match="First game") as e:
        club_players("3375", None, html=lxml.html.tostring(page))
    assert (e.value.status_code, e.value.url) == (502, url)


def test_club_players_national_team_missing_cell_raises():
    url = CLUB_PLAYERS_URL.format(club_id="3375", season="")
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (td,) = page.xpath("(//div[@id='yw1']//table[@class='items']/tbody/tr)[3]/td[7]")
    td.getparent().remove(td)
    with pytest.raises(UpstreamError) as e:
        club_players("3375", None, html=lxml.html.tostring(page))
    assert (e.value.status_code, e.value.url) == (502, url)
    assert e.value.reason == "Squad row 3 has 9 cells for 10 columns and 1 player links"


def competition_search(query: str, html: bytes | None = None) -> list[dict]:
    """Parse the recorded first results page of query (or the given html) as the endpoint returns it."""
    service_url = TransfermarktCompetitionSearch.URL_TEMPLATE.format(query=query.replace(" ", "%20"), page_number=1)
    html = html if html is not None else fixture_bytes(service_url)
    raw = TransfermarktCompetitionSearch.from_bytes(html, query=query).search_competitions()
    return CompetitionSearch.model_validate(raw).model_dump(mode="json", by_alias=True, exclude_none=True)["results"]


def test_competition_search_international_competitions():
    results = competition_search("euro")
    assert len(results) == 10
    assert results[0] == {
        "id": "EURO",
        "name": "UEFA Euro",
        "clubs": 24,
        "players": 668,
        "totalMarketValue": 12_650_000_000,
        "meanMarketValue": 527_280_000,
        "continent": "UEFA",
    }
    assert all("country" not in r for r in results)
    assert competition_search("world cup")[0]["id"] == "FIWC"
    assert competition_search("world cup")[0]["name"] == "World Cup"
    assert [r["id"] for r in competition_search("nations")][:2] == ["UNLA", "UNFI"]


def test_competition_search_row_without_flag_keeps_other_countries():
    url = TransfermarktCompetitionSearch.URL_TEMPLATE.format(query="premier", page_number=1)
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (img,) = page.xpath("//a[contains(@href, '/wettbewerb/RU1')]/ancestor::tr[1]/td[@class='zentriert'][1]//img")
    img.getparent().remove(img)
    results = {r["id"]: r for r in competition_search("premier", html=lxml.html.tostring(page))}
    assert "country" not in results["RU1"]
    assert (results["FR1"]["country"], results["UKR1"]["country"]) == ("France", "Ukraine")


def test_competition_search_missing_cell_raises():
    url = TransfermarktCompetitionSearch.URL_TEMPLATE.format(query="premier", page_number=1)
    page = lxml.html.document_fromstring(fixture_bytes(url))
    (td,) = page.xpath("//a[contains(@href, '/wettbewerb/RU1')]/ancestor::tr[1]/td[@class='rechts']")
    td.getparent().remove(td)
    with pytest.raises(UpstreamError) as e:
        competition_search("premier", html=lxml.html.tostring(page))
    assert (e.value.status_code, e.value.url) == (502, url)
    assert e.value.reason == "Competition search row 3 has 7 cells for 8 columns and 1 competition links"


def test_club_players_national_team_status():
    players = players_by_name(club_players("3375", "2024", season_id="2024"))
    assert players["Rodri"].status == "Team captain"
    assert players["Dani Vivian"].status == "Hamstring injury - Return expected on 05/10/2026"
    assert players["Pedri"].status is None


def test_club_players_unknown_club_is_404():
    # The unknown club's redirect target ("Most valuable clubs") has no h1 today; with one it must still be a 404.
    page = lxml.html.document_fromstring(fixture_bytes(CLUB_PLAYERS_URL.format(club_id="0", season="")))
    assert page.body is not None
    page.body.insert(0, lxml.html.fragment_fromstring("<header><h1>Most valuable clubs</h1></header>"))
    with pytest.raises(HTTPException) as e:
        club_players("0", None, html=lxml.html.tostring(page))
    assert e.value.status_code == 404


def test_club_players_page_of_another_club_is_404():
    with pytest.raises(HTTPException) as e:
        club_players("13", None, html=fixture_bytes(CLUB_PLAYERS_URL.format(club_id="131", season="")))
    assert e.value.status_code == 404
