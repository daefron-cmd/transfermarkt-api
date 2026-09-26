import json
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services import base
from app.services.base import TransfermarktBase, _response_from_cache
from tests.endpoints.cases import FIXTURES_DIR, FIXTURES_INDEX, RECORD_COMMAND


@pytest.fixture(scope="session")
def fixture_index() -> dict[str, dict[str, Any]]:
    return {entry["url"]: entry for entry in json.loads(FIXTURES_INDEX.read_text())}


@pytest.fixture
def unrecorded_urls(monkeypatch, fixture_index) -> list[str]:
    """Serve upstream requests from tests/fixtures; collect URLs that have no recording."""
    missing: list[str] = []

    def replay_make_request(self: TransfermarktBase, url: str | None = None):
        target = url if url else self.URL
        entry = fixture_index.get(target)
        if entry is None:
            missing.append(target)
            raise AssertionError(f"No recorded fixture for {target}. Record it with: {RECORD_COMMAND} --case NAME")
        if "file" not in entry:
            raise HTTPException(status_code=entry["status"], detail=entry["detail"])
        response = _response_from_cache((FIXTURES_DIR / entry["file"]).read_bytes(), target)
        response.status_code = entry["status"]
        return response

    monkeypatch.setattr(base, "_cache", None)
    monkeypatch.setattr(TransfermarktBase, "make_request", replay_make_request)
    return missing


@pytest.fixture
def client(unrecorded_urls) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)
