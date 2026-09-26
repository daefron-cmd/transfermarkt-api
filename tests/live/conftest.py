import functools
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main
from app.http import TransfermarktClient
from app.settings import settings


@pytest.fixture
def client(monkeypatch) -> Iterator[TestClient]:
    """The app with the real TransfermarktClient against the live site; the cache is off so every run hits the site."""
    config = settings.model_copy(update={"CACHE_ENABLE": False})
    monkeypatch.setattr(main, "TransfermarktClient", functools.partial(TransfermarktClient, config))
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client
