"""Helpers shared by the parser tests of the tmapi-backed services, on the recorded fixtures."""

import json
from typing import Any

from app.http import UpstreamError, UpstreamResponse
from tests.endpoints.cases import FIXTURES_DIR, FIXTURES_INDEX


def fixture_bytes(url: str) -> bytes:
    entry = next(e for e in json.loads(FIXTURES_INDEX.read_text()) if e["url"] == url)
    return (FIXTURES_DIR / entry["file"]).read_bytes()


def envelope(data: Any) -> bytes:
    return json.dumps({"success": True, "message": "OK", "data": data}).encode()


class NoRequestClient:
    """A client that fails the test on any request."""

    tmapi_attributes = None

    async def get(self, url: str):
        raise AssertionError(f"unexpected request {url}")


class RecordingClient:
    """A client that serves every recorded URL (raising the recorded upstream errors) and records the requested URLs."""

    tmapi_attributes = None

    def __init__(self):
        self.urls: list[str] = []

    async def get(self, url: str) -> UpstreamResponse:
        self.urls.append(url)
        entry = next(e for e in json.loads(FIXTURES_INDEX.read_text()) if e["url"] == url)
        if "file" not in entry:
            raise UpstreamError(entry["status"], url, entry["reason"])
        return UpstreamResponse(url=url, status_code=200, content=(FIXTURES_DIR / entry["file"]).read_bytes())
