import functools
import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from app import main
from app.http import TransfermarktClient, UpstreamError
from app.settings import settings
from tests.endpoints.cases import FIXTURES_DIR, FIXTURES_INDEX, RECORD_COMMAND


@pytest.fixture(scope="session")
def fixture_index() -> dict[str, dict[str, Any]]:
    return {str(httpx2.URL(entry["url"])): entry for entry in json.loads(FIXTURES_INDEX.read_text())}


@pytest.fixture
def unrecorded_urls(monkeypatch, fixture_index) -> list[str]:
    """Serve upstream requests from tests/fixtures through the real client; collect URLs that have no recording."""
    missing: list[str] = []

    def replay(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        entry = fixture_index.get(url)
        if entry is None:
            missing.append(url)
            raise AssertionError(f"No recorded fixture for {url}. Record it with: {RECORD_COMMAND} --case NAME")
        if "file" not in entry:
            raise UpstreamError(entry["status"], url, entry["reason"])
        return httpx2.Response(entry["status"], content=(FIXTURES_DIR / entry["file"]).read_bytes(), request=request)

    config = settings.model_copy(update={"CACHE_ENABLE": False, "OUTBOUND_MIN_INTERVAL_MS": 0})
    transport = httpx2.MockTransport(replay)
    monkeypatch.setattr(main, "TransfermarktClient", functools.partial(TransfermarktClient, config, transport))
    return missing


@pytest.fixture
def client(unrecorded_urls) -> Iterator[TestClient]:
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client
