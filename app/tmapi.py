"""
Helpers for tmapi.transfermarkt.technology, the unofficial and undocumented JSON API behind Transfermarkt's web
components. Every response is an envelope {"success": bool, "message": str, "data": ...}; anything else is treated
as an upstream error, since the API may change without notice.
"""

import json
from collections.abc import Iterable
from typing import Any

from app.http import TransfermarktClient, UpstreamError

TMAPI_URL = "https://tmapi.transfermarkt.technology"
BATCH_SIZE = 50


def tmapi_data(url: str, content: bytes, *, failure_status: int = 502) -> Any:
    """
    Unwrap a tmapi envelope and return its `data`.

    Args:
        url (str): The requested URL (used in error details).
        content (bytes): The raw response body.
        failure_status (int): The status code to raise with when the envelope reports success=false.

    Raises:
        UpstreamError: If the body is not a JSON envelope with a `data` member (502) or reports success=false
            (`failure_status`).
    """
    try:
        body = json.loads(content)
    except ValueError as e:
        raise UpstreamError(502, url, "Unexpected tmapi response: body is not JSON") from e
    if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
        raise UpstreamError(502, url, "Unexpected tmapi response: no success flag")
    if not body["success"]:
        raise UpstreamError(failure_status, url, f"tmapi request failed ({body.get('message') or 'no message'})")
    if "data" not in body:
        raise UpstreamError(502, url, "Unexpected tmapi response: no data")
    return body["data"]


def check_fields(url: str, obj: Any, fields: Iterable[tuple[str, type | tuple[type, ...]]], where: str) -> None:
    """
    Check that each dotted path in `fields` exists in `obj` and holds a value of the given type(s); a bool does not
    count as an int.

    Raises:
        UpstreamError: 502 naming the first offending path and `where` (e.g. "game 4359338").
    """
    for path, expected in fields:
        types = expected if isinstance(expected, tuple) else (expected,)
        value: Any = obj
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                raise UpstreamError(502, url, f"Unexpected tmapi response: no {path} in {where}")
            value = value[key]
        if not isinstance(value, types) or (isinstance(value, bool) and bool not in types):
            raise UpstreamError(502, url, f"Unexpected tmapi response: {path}={value!r} in {where}")


def parse_competition(url: str, content: bytes) -> tuple[str, int]:
    """
    Return the name and current season id of a competition from a /competition/{id} response.

    Raises:
        UpstreamError: 404 if tmapi reports success=false, 502 if the response does not have the expected shape.
    """
    data = tmapi_data(url, content, failure_status=404)
    check_fields(url, data, [("name", str), ("currentSeasonId", int)], "competition")
    return data["name"].strip(), data["currentSeasonId"]


def parse_club(url: str, content: bytes) -> tuple[str, bool]:
    """
    Return the name of a club and whether it is a national team from a /club/{id} response.

    Raises:
        UpstreamError: 404 if tmapi reports success=false, 502 if the response does not have the expected shape.
    """
    data = tmapi_data(url, content, failure_status=404)
    check_fields(url, data, [("name", str), ("baseDetails.isNationalTeam", bool)], "club")
    return data["name"].strip(), data["baseDetails"]["isNationalTeam"]


def batch_urls(resource: str, ids: Iterable[str]) -> list[str]:
    """Build batch lookup URLs such as /clubs?ids[]=1&ids[]=2, with ids deduplicated, sorted and chunked."""
    unique = sorted(set(ids))
    chunks = [unique[i : i + BATCH_SIZE] for i in range(0, len(unique), BATCH_SIZE)]
    return [f"{TMAPI_URL}/{resource}?" + "&".join(f"ids[]={id_}" for id_ in chunk) for chunk in chunks]


def names_by_id(url: str, content: bytes) -> dict[str, str]:
    """Map id to name (whitespace stripped) from a batch lookup response (a list of {id, name, ...})."""
    data = tmapi_data(url, content)
    if not isinstance(data, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("id") is not None for item in data
    ):
        raise UpstreamError(502, url, "Unexpected tmapi response: lookup data is not a list of {id, name}")
    return {str(item["id"]): item["name"].strip() for item in data}


async def lookup_names(client: TransfermarktClient, resource: str, ids: Iterable[str]) -> dict[str, str]:
    """
    Fetch the names of the given ids from a tmapi batch lookup resource ("clubs", "competitions", ...).

    Raises:
        UpstreamError: If a response is malformed or an id is missing from the results.
    """
    requested = set(ids)
    names: dict[str, str] = {}
    for url in batch_urls(resource, requested):
        response = await client.get(url)
        names.update(names_by_id(response.url, response.content))
    missing = sorted(requested - names.keys())
    if missing:
        raise UpstreamError(502, f"{TMAPI_URL}/{resource}", f"tmapi lookup is missing ids {missing}")
    return names


async def get_attributes(client: TransfermarktClient) -> dict[str, Any]:
    """Fetch the tmapi attribute tables (positions, absences, ...), cached on the client for its lifetime."""
    if client.tmapi_attributes is None:
        response = await client.get(f"{TMAPI_URL}/attributes")
        data = tmapi_data(response.url, response.content)
        if not isinstance(data, dict):
            raise UpstreamError(502, response.url, "Unexpected tmapi response: attributes data is not an object")
        client.tmapi_attributes = data
    return client.tmapi_attributes
