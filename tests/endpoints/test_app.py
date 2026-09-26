"""App-level behaviour: health, errors, logging, rate limiting, proxy headers, CORS and the OpenAPI schema."""

import importlib
import logging
from collections.abc import Iterator

import limits
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__, main
from app.services.players.profile import TransfermarktPlayerProfile
from app.settings import settings


@pytest.fixture
def rate_limited(monkeypatch) -> Iterator[int]:
    """Enable the inbound rate limiter with empty storage; returns the number of requests allowed per window."""
    monkeypatch.setattr(main.limiter, "enabled", True)
    main.limiter.reset()
    yield limits.parse(settings.RATE_LIMITING_FREQUENCY).amount
    main.limiter.reset()


@pytest.fixture
def cors_client(request) -> Iterator[TestClient]:
    """The app rebuilt with CORS_ORIGINS set; the module is rebuilt from the original settings afterwards."""
    original = settings.CORS_ORIGINS
    settings.CORS_ORIGINS = "https://a.example, https://b.example"
    importlib.reload(main)
    try:
        request.getfixturevalue("unrecorded_urls")
        with TestClient(main.app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        settings.CORS_ORIGINS = original
        importlib.reload(main)


def test_version():
    assert __version__ == "4.0.1"
    assert main.app.version == __version__


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_health_is_not_rate_limited(client, rate_limited):
    for _ in range(rate_limited + 3):
        assert client.get("/health").status_code == 200


def test_rate_limit_error_shape(client, rate_limited):
    statuses = [client.get("/openapi.json").status_code for _ in range(rate_limited)]
    response = client.get("/openapi.json")

    assert statuses == [200] * rate_limited
    assert response.status_code == 429
    assert list(response.json()) == ["detail"]
    assert response.json()["detail"].startswith("Rate limit exceeded")


def test_limiter_keys_on_forwarded_address_behind_trusted_proxy(unrecorded_urls, rate_limited):
    # Uvicorn's --proxy-headers wraps the app in this middleware; "*" stands for FORWARDED_ALLOW_IPS=*.
    with TestClient(ProxyHeadersMiddleware(main.app, trusted_hosts="*")) as proxied:
        for _ in range(rate_limited):
            assert proxied.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
        blocked = proxied.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.1"})
        other = proxied.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.2"})

    assert blocked.status_code == 429
    assert other.status_code == 200


def test_limiter_ignores_forwarded_address_from_untrusted_proxy(unrecorded_urls, rate_limited):
    # The TestClient connects as "testclient", which is not in the trusted hosts, so X-Forwarded-For is ignored.
    with TestClient(ProxyHeadersMiddleware(main.app, trusted_hosts="127.0.0.1")) as proxied:
        for _ in range(rate_limited):
            assert proxied.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
        other = proxied.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.2"})

    assert other.status_code == 429


@pytest.mark.parametrize(
    "method,path,status,detail",
    [
        ("GET", "/nope", 404, "Not Found"),
        ("POST", "/health", 405, "Method Not Allowed"),
    ],
)
def test_error_shape(client, method, path, status, detail):
    response = client.request(method, path)

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_unexpected_exception_is_a_generic_500(client, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("secret upstream internals")

    monkeypatch.setattr(TransfermarktPlayerProfile, "fetch", boom)
    with caplog.at_level(logging.ERROR):
        response = client.get("/players/28003/profile")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret" not in response.text
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert len(errors) == 1
    assert "GET /players/28003/profile" in errors[0].getMessage()
    assert errors[0].exc_info[1].args == ("secret upstream internals",)


def test_one_log_line_per_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.requests"):
        client.get("/health")
        client.get("/nope")

    lines = [r.getMessage() for r in caplog.records if r.name == "app.requests"]
    assert len(lines) == 2
    assert lines[0].startswith("GET /health 200 ")
    assert lines[0].endswith("ms")
    assert lines[1].startswith("GET /nope 404 ")


def test_logging_is_configured_at_startup(client):
    root = logging.getLogger()
    handlers = [h for h in root.handlers if h.get_name() == main.LOG_HANDLER_NAME]

    assert root.level == logging.getLevelNamesMapping()[settings.LOG_LEVEL]
    assert len(handlers) == 1
    assert handlers[0].formatter is not None
    assert handlers[0].formatter._fmt == "%(asctime)s %(levelname)s %(name)s %(message)s"
    assert logging.getLogger("uvicorn.access").disabled
    # httpx2 logs every request at INFO; TransfermarktClient logs upstream fetches itself (at DEBUG).
    assert logging.getLogger("httpx2").level == logging.WARNING
    assert logging.getLogger("httpcore2").level == logging.WARNING


def test_cors_disabled_by_default(client):
    assert settings.CORS_ORIGINS == ""
    response = client.get("/health", headers={"Origin": "https://a.example"})

    assert "access-control-allow-origin" not in response.headers


def test_cors_enabled(cors_client):
    allowed = cors_client.get("/health", headers={"Origin": "https://b.example"})
    denied = cors_client.get("/health", headers={"Origin": "https://evil.example"})
    preflight = cors_client.options(
        "/health", headers={"Origin": "https://a.example", "Access-Control-Request-Method": "GET"}
    )

    assert allowed.headers["access-control-allow-origin"] == "https://b.example"
    assert "access-control-allow-origin" not in denied.headers
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://a.example"


def test_openapi_schema(client):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["version"] == __version__
    assert "unofficial" in schema["info"]["description"].lower()
    operations = {
        (method, path): operation for path, item in schema["paths"].items() for method, operation in item.items()
    }
    assert ("get", "/health") in operations
    for key, operation in operations.items():
        assert operation.get("summary"), key
        assert operation.get("description"), key

    def param(path: str, name: str) -> dict:
        return next(p for p in operations[("get", path)]["parameters"] if p["name"] == name)

    assert param("/players/{player_id}/stats", "season_id")["description"]
    assert param("/players/{player_id}/stats", "season_id")["schema"]["anyOf"][0]["pattern"] == "^[0-9]{4}$"
    for path in (
        "/players/search/{player_name}",
        "/clubs/search/{club_name}",
        "/competitions/search/{competition_name}",
    ):
        assert param(path, "page_number")["description"], path
        assert param(path, "page_number")["schema"]["minimum"] == 1, path
    assert param("/players/{player_id}/profile", "player_id")["schema"]["pattern"] == "^[0-9]+$"
    assert param("/clubs/{club_id}/profile", "club_id")["schema"]["maxLength"] == 12
    assert param("/competitions/{competition_id}/clubs", "competition_id")["schema"]["pattern"] == "^[A-Za-z0-9]{1,12}$"
