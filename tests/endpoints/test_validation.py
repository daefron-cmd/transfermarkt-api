import pytest

from app.http import TransfermarktClient


@pytest.fixture
def upstream_calls(monkeypatch, unrecorded_urls) -> list[str]:
    """Every URL passed to TransfermarktClient.get, recorded or not."""
    calls: list[str] = []
    real_get = TransfermarktClient.get

    async def recording_get(self, url):
        calls.append(url)
        return await real_get(self, url)

    monkeypatch.setattr(TransfermarktClient, "get", recording_get)
    return calls


@pytest.mark.parametrize(
    "path",
    [
        "/players/abc/profile",
        "/players/28003..foo/profile",
        "/players/28003%20/profile",
        "/players/1234567890123/market_value",
        "/players/-1/transfers",
        "/players/abc/jersey_numbers",
        "/players/abc/stats",
        "/players/abc/injuries",
        "/players/abc/achievements",
        "/players/937958/stats?season_id=None",
        "/players/937958/stats?season_id=24",
        "/players/search/messi?page_number=0",
        "/players/search/messi?page_number=abc",
        "/players/28003/injuries?page_number=0",
        "/clubs/abc/profile",
        "/clubs/abc/players",
        "/clubs/131/players?season_id=14",
        "/clubs/131/players?season_id=abcd",
        "/clubs/search/barcelona?page_number=-1",
        "/competitions/ES-1/clubs",
        "/competitions/ABCDEFGHIJKLM/clubs",
        "/competitions/ES1/clubs?season_id=20231",
        "/competitions/search/premier?page_number=0",
    ],
)
def test_invalid_input_is_422_without_upstream_request(path, client, unrecorded_urls, upstream_calls):
    response = client.get(path)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert upstream_calls == []
    assert unrecorded_urls == []


def test_encoded_slash_in_id_never_reaches_a_route(client, unrecorded_urls, upstream_calls):
    # The server decodes %2F before routing, so the id splits into extra path segments and no route matches.
    response = client.get("/players/28003%2F..%2Ffoo/profile")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert upstream_calls == []
    assert unrecorded_urls == []


@pytest.mark.parametrize(
    "path",
    [
        "/players/28003/profile",
        "/players/search/messi?page_number=2",
        "/players/937958/stats?season_id=2024",
        "/clubs/131/players?season_id=2014",
        "/competitions/ES1/clubs?season_id=2023",
    ],
)
def test_valid_input_still_works(path, client, unrecorded_urls, upstream_calls):
    response = client.get(path)

    assert response.status_code == 200
    assert upstream_calls
    assert unrecorded_urls == []
