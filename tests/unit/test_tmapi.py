"""Tests for the tmapi envelope and lookup helpers (app.tmapi): the exact error at each raise site."""

import asyncio

import pytest

from app.http import UpstreamError, UpstreamResponse
from app.tmapi import (
    TMAPI_URL,
    check_fields,
    get_attributes,
    lookup_names,
    names_by_id,
    parse_club,
    parse_competition,
    tmapi_data,
)
from tests.unit.tmapi_helpers import envelope

URL = "https://tmapi.example/x"


class StubClient:
    """A client that answers each URL with the given body."""

    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.tmapi_attributes = None

    async def get(self, url: str) -> UpstreamResponse:
        return UpstreamResponse(url=url, status_code=200, content=self.bodies[url])


def raised(call) -> UpstreamError:
    with pytest.raises(UpstreamError) as e:
        call()
    return e.value


@pytest.mark.parametrize(
    "body,status,reason",
    [
        (b"<html>", 502, "Unexpected tmapi response: body is not JSON"),
        (b"[]", 502, "Unexpected tmapi response: no success flag"),
        (b'{"success": 1}', 502, "Unexpected tmapi response: no success flag"),
        (b'{"success": false, "message": "Club not found"}', 502, "tmapi request failed (Club not found)"),
        (b'{"success": false, "message": ""}', 502, "tmapi request failed (no message)"),
        (b'{"success": false}', 502, "tmapi request failed (no message)"),
        (b'{"success": true}', 502, "Unexpected tmapi response: no data"),
    ],
)
def test_tmapi_data_errors(body, status, reason):
    error = raised(lambda: tmapi_data(URL, body))
    assert (error.status_code, error.detail) == (status, f"{reason} for url: {URL}")


def test_tmapi_data_failure_status():
    error = raised(lambda: tmapi_data(URL, b'{"success": false, "message": "Not found"}', failure_status=404))
    assert (error.status_code, error.detail) == (404, f"tmapi request failed (Not found) for url: {URL}")


@pytest.mark.parametrize(
    "obj,reason",
    [
        ({"a": {"c": 1}}, "Unexpected tmapi response: no a.b in game 1"),
        ({"a": 1}, "Unexpected tmapi response: no a.b in game 1"),
        ({"a": {"b": "1"}}, "Unexpected tmapi response: a.b='1' in game 1"),
        ({"a": {"b": True}}, "Unexpected tmapi response: a.b=True in game 1"),
    ],
)
def test_check_fields_errors(obj, reason):
    error = raised(lambda: check_fields(URL, obj, [("a.b", int)], "game 1"))
    assert (error.status_code, error.detail) == (502, f"{reason} for url: {URL}")


def test_check_fields_accepts_every_type_of_a_tuple_including_bool():
    fields = [("a", (bool, type(None))), ("b", (bool, type(None))), ("c", (int, str))]
    check_fields(URL, {"a": True, "b": None, "c": "x"}, fields, "game 1")


def test_parse_competition_error_names_the_competition():
    error = raised(lambda: parse_competition(URL, envelope({"name": "LaLiga", "currentSeasonId": "2024"})))
    assert error.detail == f"Unexpected tmapi response: currentSeasonId='2024' in competition for url: {URL}"


def test_parse_club_errors_name_the_url_and_the_club():
    error = raised(lambda: parse_club(URL, b'{"success": false, "message": "Club not found"}'))
    assert (error.status_code, error.detail) == (404, f"tmapi request failed (Club not found) for url: {URL}")
    error = raised(lambda: parse_club(URL, envelope({"name": "FC Barcelona", "baseDetails": {"isNationalTeam": 0}})))
    assert error.detail == f"Unexpected tmapi response: baseDetails.isNationalTeam=0 in club for url: {URL}"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"1": "a"},
        [{"id": "1", "name": "a"}, {"id": "2"}],
        [{"id": "1", "name": "a"}, {"name": "b"}],
        [{"id": "1", "name": "a"}, {"id": None, "name": "b"}],
        [{"id": "1", "name": "a"}, "b"],
    ],
)
def test_names_by_id_rejects_anything_but_a_list_of_id_and_name(data):
    error = raised(lambda: names_by_id(URL, envelope(data)))
    assert (error.status_code, error.detail) == (
        502,
        f"Unexpected tmapi response: lookup data is not a list of {{id, name}} for url: {URL}",
    )


def test_names_by_id_error_names_the_url():
    error = raised(lambda: names_by_id(URL, b"[]"))
    assert error.detail == f"Unexpected tmapi response: no success flag for url: {URL}"


def test_lookup_names_malformed_response_names_the_batch_url():
    url = f"{TMAPI_URL}/clubs?ids[]=1&ids[]=2"
    client = StubClient({url: envelope([{"id": 1, "name": "a"}, {"id": 2}])})
    error = raised(lambda: asyncio.run(lookup_names(client, "clubs", ["2", "1"])))  # type: ignore[arg-type]
    assert error.detail == f"Unexpected tmapi response: lookup data is not a list of {{id, name}} for url: {url}"


def test_lookup_names_missing_ids_name_the_resource():
    url = f"{TMAPI_URL}/clubs?ids[]=1&ids[]=2&ids[]=3"
    client = StubClient({url: envelope([{"id": 2, "name": "b"}])})
    error = raised(lambda: asyncio.run(lookup_names(client, "clubs", ["3", "1", "2"])))  # type: ignore[arg-type]
    assert (error.status_code, error.detail) == (
        502,
        f"tmapi lookup is missing ids ['1', '3'] for url: {TMAPI_URL}/clubs",
    )


@pytest.mark.parametrize(
    "body,reason",
    [
        (b"[]", "Unexpected tmapi response: no success flag"),
        (envelope([{"id": "1"}]), "Unexpected tmapi response: attributes data is not an object"),
    ],
)
def test_get_attributes_errors_name_the_url(body, reason):
    url = f"{TMAPI_URL}/attributes"
    client = StubClient({url: body})
    error = raised(lambda: asyncio.run(get_attributes(client)))  # type: ignore[arg-type]
    assert (error.status_code, error.detail) == (502, f"{reason} for url: {url}")
    assert client.tmapi_attributes is None
