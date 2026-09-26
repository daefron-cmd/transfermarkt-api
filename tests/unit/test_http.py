import asyncio
import logging
import types
from collections.abc import Callable

import httpx2
import pytest

from app import http
from app.http import TransfermarktClient, UpstreamError
from app.settings import settings

URL = "https://www.transfermarkt.com/-/profil/spieler/28003"


def make_client(tmp_path, handler: Callable[[httpx2.Request], httpx2.Response], **overrides) -> TransfermarktClient:
    config = settings.model_copy(
        update={
            "CACHE_ENABLE": False,
            "CACHE_DIR": str(tmp_path / "cache"),
            "OUTBOUND_MIN_INTERVAL_MS": 0,
            **overrides,
        },
    )
    return TransfermarktClient(config, httpx2.MockTransport(handler))


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record asyncio.sleep delays instead of waiting (tenacity and the throttle both sleep through asyncio)."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def test_cache_hit_avoids_second_request(tmp_path):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(200, content=b"<html>ok</html>")

    async def run() -> tuple[bytes, bytes]:
        async with make_client(tmp_path, handler, CACHE_ENABLE=True) as client:
            first = await client.get(URL)
            second = await client.get(URL)
        return first.content, second.content

    assert asyncio.run(run()) == (b"<html>ok</html>", b"<html>ok</html>")
    assert requests == [URL]


def test_429_is_retried_and_honours_retry_after(tmp_path, sleeps):
    statuses = iter([429, 200])

    def handler(request: httpx2.Request) -> httpx2.Response:
        status = next(statuses)
        headers = {"Retry-After": "7"} if status == 429 else {}
        return httpx2.Response(status, headers=headers, content=b"body")

    async def run() -> int:
        async with make_client(tmp_path, handler) as client:
            return (await client.get(URL)).status_code

    assert asyncio.run(run()) == 200
    assert sleeps == [7.0]


def test_404_raises_upstream_error_and_is_not_cached(tmp_path):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(404)

    async def run() -> list[UpstreamError]:
        errors = []
        async with make_client(tmp_path, handler, CACHE_ENABLE=True) as client:
            for _ in range(2):
                with pytest.raises(UpstreamError) as exc_info:
                    await client.get(URL)
                errors.append(exc_info.value)
        return errors

    errors = asyncio.run(run())
    assert [e.status_code for e in errors] == [404, 404]
    assert errors[0].detail == f"Client Error. Not Found for url: {URL}"
    assert len(requests) == 2


def test_throttle_spaces_requests(tmp_path, monkeypatch, sleeps):
    clock = [100.0]

    async def advancing_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(asyncio, "sleep", advancing_sleep)
    monkeypatch.setattr(http, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"ok")

    async def run() -> None:
        async with make_client(tmp_path, handler, OUTBOUND_MIN_INTERVAL_MS=500) as client:
            for page in range(3):
                await client.get(f"{URL}?page={page}")

    asyncio.run(run())
    assert sleeps == [0.5, 0.5]


def test_fetches_are_logged(tmp_path, sleeps, caplog):
    statuses = iter([503, 200])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(statuses), content=b"body")

    async def run() -> None:
        async with make_client(tmp_path, handler, CACHE_ENABLE=True) as client:
            await client.get(URL)
            await client.get(URL)

    with caplog.at_level(logging.DEBUG, logger="app.http"):
        asyncio.run(run())

    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "app.http"]
    assert records == [
        (logging.DEBUG, f"GET {URL} cache=miss status=503 attempt=1"),
        (logging.WARNING, f"Retrying GET {URL} after attempt 1 (503 Service Unavailable) in {sleeps[0]:.1f}s"),
        (logging.DEBUG, f"GET {URL} cache=miss status=200 attempt=2"),
        (logging.DEBUG, f"GET {URL} cache=hit"),
    ]


def test_transport_error_retry_is_logged(tmp_path, sleeps, caplog):
    calls = iter([httpx2.ConnectError("refused"), None])

    def handler(request: httpx2.Request) -> httpx2.Response:
        error = next(calls)
        if error is not None:
            raise error
        return httpx2.Response(200, content=b"body")

    async def run() -> None:
        async with make_client(tmp_path, handler) as client:
            await client.get(URL)

    with caplog.at_level(logging.DEBUG, logger="app.http"):
        asyncio.run(run())

    warnings = [r.getMessage() for r in caplog.records if r.name == "app.http" and r.levelno == logging.WARNING]
    assert warnings == [f"Retrying GET {URL} after attempt 1 (ConnectError: refused) in {sleeps[0]:.1f}s"]
