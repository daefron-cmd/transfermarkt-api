import asyncio
import logging
import time
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

    async def run() -> tuple[http.UpstreamResponse, http.UpstreamResponse]:
        async with make_client(tmp_path, handler, CACHE_ENABLE=True) as client:
            first = await client.get(URL)
            second = await client.get(URL)
        return first, second

    first, second = asyncio.run(run())
    assert (first.content, second.content) == (b"<html>ok</html>", b"<html>ok</html>")
    assert (second.url, second.status_code) == (URL, 200)
    assert requests == [URL]


def test_cached_body_expires_after_ttl(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"body")

    async def run() -> float:
        async with make_client(tmp_path, handler, CACHE_ENABLE=True, CACHE_TTL_SECONDS=123) as client:
            await client.get(URL)
            assert client._cache is not None
            entry = client._cache.get(URL, expire_time=True)
            assert isinstance(entry, tuple)
            expire_time = entry[1]
            assert isinstance(expire_time, float)
            return expire_time

    assert 100 < asyncio.run(run()) - time.time() <= 123


def test_client_is_configured_from_settings(tmp_path, monkeypatch):
    # Without a transport httpx builds its own pool from `limits`. NO_PROXY keeps it from reading the macOS system
    # proxy settings, which aborts inside mutmut's forked workers.
    monkeypatch.setenv("NO_PROXY", "*")
    config = settings.model_copy(
        update={
            "CACHE_ENABLE": True,
            "CACHE_DIR": str(tmp_path / "cache"),
            "CACHE_SIZE_LIMIT_MB": 7,
            "OUTBOUND_MAX_CONCURRENCY": 3,
            "OUTBOUND_TIMEOUT_S": 12.5,
        },
    )
    client = TransfermarktClient(config)
    try:
        transport = client._http._transport
        assert isinstance(transport, httpx2.AsyncHTTPTransport)
        pool = transport._pool
        assert client._http.timeout == httpx2.Timeout(12.5)
        assert client._http.follow_redirects is True
        assert (pool._max_connections, pool._max_keepalive_connections) == (3, 3)
        assert client._cache is not None
        assert client._cache.directory == str(tmp_path / "cache")
        # The ignore: diskcache exposes its settings as dynamic attributes that its (absent) stubs cannot declare.
        assert client._cache.size_limit == 7 * 1024 * 1024  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        asyncio.run(client.aclose())


def test_requests_carry_one_rotating_user_agent(tmp_path):
    user_agents: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        user_agents.append(request.headers.get_list("User-Agent"))
        return httpx2.Response(200, content=b"body")

    async def run() -> None:
        async with make_client(tmp_path, handler) as client:
            await client.get(URL)

    asyncio.run(run())
    assert len(user_agents) == 1
    assert len(user_agents[0]) == 1
    assert user_agents[0][0] in http.USER_AGENTS


def test_redirects_are_followed(tmp_path):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        if request.url.path == "/old":
            return httpx2.Response(301, headers={"Location": "https://www.transfermarkt.com/new"})
        return httpx2.Response(200, content=b"moved")

    async def run() -> bytes:
        async with make_client(tmp_path, handler) as client:
            return (await client.get("https://www.transfermarkt.com/old")).content

    assert asyncio.run(run()) == b"moved"
    assert requests == ["/old", "/new"]


def get_error(tmp_path, handler: Callable[[httpx2.Request], httpx2.Response], **overrides) -> UpstreamError:
    async def run() -> UpstreamError:
        async with make_client(tmp_path, handler, **overrides) as client:
            with pytest.raises(UpstreamError) as exc_info:
                await client.get(URL)
        return exc_info.value

    return asyncio.run(run())


def test_redirect_loop_is_a_404(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": URL})

    error = get_error(tmp_path, handler)
    assert (error.status_code, error.url, error.reason) == (404, URL, "Not found")


def test_400_is_a_client_error(tmp_path):
    error = get_error(tmp_path, lambda request: httpx2.Response(400))
    assert (error.status_code, error.url, error.reason) == (400, URL, "Client Error. Bad Request")


def test_500_is_retried(tmp_path, sleeps):
    statuses = iter([500, 200])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(statuses), content=b"body")

    async def run() -> int:
        async with make_client(tmp_path, handler) as client:
            return (await client.get(URL)).status_code

    assert asyncio.run(run()) == 200
    assert len(sleeps) == 1


def test_transient_status_after_last_retry_keeps_its_status(tmp_path, sleeps):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(503)

    error = get_error(tmp_path, handler, OUTBOUND_MAX_RETRIES=2)
    assert (error.status_code, error.url) == (503, URL)
    assert error.reason == "Upstream error after retries. Service Unavailable"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("exception", "status_code", "reason"),
    [
        (httpx2.ReadTimeout("slow"), 502, "Timeout"),
        (httpx2.ConnectError("refused"), 502, "Connection error"),
    ],
)
def test_transport_failure_after_last_retry(tmp_path, sleeps, exception, status_code, reason):
    attempts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(str(request.url))
        raise exception

    error = get_error(tmp_path, handler, OUTBOUND_MAX_RETRIES=2)
    assert (error.status_code, error.url, error.reason) == (status_code, URL, reason)
    assert len(attempts) == 2


def test_other_http_error_is_a_500_without_retry(tmp_path, sleeps):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.DecodingError("bad gzip")

    error = get_error(tmp_path, handler)
    assert (error.status_code, error.url, error.reason) == (500, URL, "Error. bad gzip")
    assert sleeps == []


def test_no_attempt_is_a_500(tmp_path, monkeypatch):
    class NoAttempts:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __aiter__(self) -> "NoAttempts":
            return self

        async def __anext__(self) -> None:
            raise StopAsyncIteration

    monkeypatch.setattr(http, "AsyncRetrying", NoAttempts)
    error = get_error(tmp_path, lambda request: httpx2.Response(200))
    assert (error.status_code, error.url, error.reason) == (500, URL, "Error. No upstream attempt was made")


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(None, None), ("soon", None), ("-5", 0.0), ("7", 7.0), ("120", 60.0)],
)
def test_parse_retry_after(value, seconds):
    assert http._parse_retry_after(value) == seconds


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


def test_throttle_does_not_sleep_once_the_interval_has_passed(tmp_path, monkeypatch, sleeps):
    clock = [100.0]
    monkeypatch.setattr(http, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))

    def handler(request: httpx2.Request) -> httpx2.Response:
        clock[0] += 0.5  # each request takes exactly the minimum interval
        return httpx2.Response(200, content=b"ok")

    async def run() -> None:
        async with make_client(tmp_path, handler, OUTBOUND_MIN_INTERVAL_MS=500) as client:
            for page in range(3):
                await client.get(f"{URL}?page={page}")

    asyncio.run(run())
    assert sleeps == []


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


@pytest.mark.parametrize("body", [b"", b" \r\n"])
def test_empty_body_is_returned_but_not_cached(tmp_path, body):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(200, content=body)

    async def run() -> tuple[list[bytes], bool]:
        async with make_client(tmp_path, handler, CACHE_ENABLE=True) as client:
            contents = [(await client.get(URL)).content for _ in range(2)]
            assert client._cache is not None
            return contents, URL in client._cache

    contents, cached = asyncio.run(run())
    assert contents == [body, body]
    assert not cached
    assert requests == [URL, URL]
