from datetime import date, timedelta

from app.schemas.base import AuditMixin
from app.schemas.clubs.profile import ClubProfile
from app.schemas.players.injuries import Injury
from app.schemas.players.market_value import PlayerRanking


def test_date_fields_are_day_first():
    squad = {"size": "1", "averageAge": "20.0", "foreigners": "0", "nationalTeamPlayers": "0"}
    profile = ClubProfile.model_validate(
        {
            "id": "1",
            "url": "/x",
            "name": "x",
            "image": "x",
            "foundedOn": "04/05/2001",
            "membersDate": "Jan 1, 2022",
            "members": "170.000",
            "squad": squad,
            "league": {},
        }
    )
    assert profile.founded_on == date(2001, 5, 4)
    assert profile.members_date == date(2022, 1, 1)
    assert profile.members == 170_000


def test_club_profile_national_team_fields_optional():
    squad = {"size": "29", "averageAge": "27.5", "foreigners": "28", "nationalTeamPlayers": None}
    profile = ClubProfile.model_validate(
        {
            "id": "3437",
            "url": "/x",
            "name": "x",
            "image": "x",
            "stadiumName": None,
            "stadiumSeats": None,
            "currentTransferRecord": None,
            "fifaWorldRanking": "2",
            "squad": squad,
            "league": {},
        }
    )
    assert profile.stadium_name is None
    assert profile.stadium_seats is None
    assert profile.current_transfer_record is None
    assert profile.squad.national_team_players is None
    assert profile.fifa_world_ranking == 2


def test_injury_dates_are_day_first():
    injury = Injury.model_validate(
        {
            "season": "25/26",
            "injury": "x",
            "fromDate": "04/05/2001",
            "untilDate": "-",
            "days": "12 days",
            "gamesMissed": "1.234",
            "gamesMissedClubs": [],
        }
    )
    assert injury.from_date == date(2001, 5, 4)
    assert injury.until_date is None
    assert injury.games_missed == 1234


def test_player_ranking_thousands():
    assert PlayerRanking.model_validate({"Worldwide": "1.234", "Spain": "7"}).root == {"Worldwide": 1234, "Spain": 7}


def test_updated_at_is_timezone_aware_utc():
    audit = AuditMixin()

    assert audit.updated_at.utcoffset() == timedelta(0)
    assert audit.model_dump_json().endswith('Z"}')
