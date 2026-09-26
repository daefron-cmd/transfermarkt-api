import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Self

import diskcache
import httpx2
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential, wait_random

from app.settings import Settings, settings

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
)

_RETRYABLE_STATUS = frozenset({408, 425, 429})
_RETRY_AFTER_CAP_S = 60.0
_TRANSIENT_TRANSPORT_ERRORS = (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RemoteProtocolError)
_backoff = wait_exponential(multiplier=1, min=1, max=30) + wait_random(0, 1)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamResponse:
    """A successful (non-error) upstream response, either fresh or served from the disk cache."""

    url: str
    status_code: int
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


class UpstreamError(Exception):
    """An upstream request that failed; app.main maps it to a JSON error response with this status code."""

    def __init__(self, status_code: int, url: str, reason: str):
        self.status_code = status_code
        self.url = url
        self.reason = reason
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        return f"{self.reason} for url: {self.url}"


class _TransientStatusError(Exception):
    def __init__(self, status_code: int, reason: str, retry_after: float | None):
        self.status_code = status_code
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"{status_code} {reason}")


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS or status_code >= 500


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a numeric Retry-After header (seconds), capped. HTTP-date values fall back to the backoff."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(max(seconds, 0.0), _RETRY_AFTER_CAP_S)


def _wait(retry_state: RetryCallState) -> float:
    """Honour the upstream Retry-After header when present, otherwise use exponential backoff with jitter."""
    error = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(error, _TransientStatusError) and error.retry_after is not None:
        return error.retry_after
    return _backoff(retry_state)


def _log_retry(url: str, retry_state: RetryCallState) -> None:
    error = retry_state.outcome.exception() if retry_state.outcome else None
    reason = str(error) if isinstance(error, _TransientStatusError) else f"{type(error).__name__}: {error}"
    delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
    logger.warning("Retrying GET %s after attempt %d (%s) in %.1fs", url, retry_state.attempt_number, reason, delay)


class _MinIntervalThrottle:
    """Process-wide minimum interval between outbound request starts. Politeness > evasion."""

    def __init__(self, min_interval_s: float):
        self._min_interval = min_interval_s
        self._last = float("-inf")
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self._min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class TransfermarktClient:
    """
    Async HTTP client for Transfermarkt: a shared connection pool with a rotating User-Agent, a minimum-interval
    throttle, bounded concurrency, retry on transient failures and a disk-backed TTL cache of successful bodies.
    """

    def __init__(self, config: Settings = settings, transport: httpx2.AsyncBaseTransport | None = None):
        self._config = config
        self._http = httpx2.AsyncClient(
            timeout=config.OUTBOUND_TIMEOUT_S,
            follow_redirects=True,
            limits=httpx2.Limits(
                max_connections=config.OUTBOUND_MAX_CONCURRENCY,
                max_keepalive_connections=config.OUTBOUND_MAX_CONCURRENCY,
            ),
            transport=transport,
        )
        self._throttle = _MinIntervalThrottle(config.OUTBOUND_MIN_INTERVAL_MS / 1000.0)
        self._semaphore = asyncio.Semaphore(config.OUTBOUND_MAX_CONCURRENCY)
        self._cache = (
            diskcache.Cache(directory=config.CACHE_DIR, size_limit=config.CACHE_SIZE_LIMIT_MB * 1024 * 1024)
            if config.CACHE_ENABLE
            else None
        )
        # tmapi attribute tables (positions, absences, ...), fetched once per client; see app.tmapi.get_attributes.
        self.tmapi_attributes: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._cache is not None:
            self._cache.close()

    async def get(self, url: str) -> UpstreamResponse:
        """
        GET a Transfermarkt URL, served from the disk cache when possible.

        Raises:
            UpstreamError: If the upstream answers with a 4xx/5xx status (after retries for transient ones), there
                are too many redirects, or the connection fails or times out.
        """
        url = str(httpx2.URL(url))

        if self._cache is not None:
            # diskcache is untyped; only bytes are ever stored, so anything else is treated as a miss.
            cached = self._cache.get(url)
            if isinstance(cached, bytes):
                logger.debug("GET %s cache=hit", url)
                return UpstreamResponse(url=url, status_code=200, content=cached)

        response: httpx2.Response | None = None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type((_TransientStatusError, *_TRANSIENT_TRANSPORT_ERRORS)),
                wait=_wait,
                before_sleep=lambda retry_state: _log_retry(url, retry_state),
                stop=stop_after_attempt(self._config.OUTBOUND_MAX_RETRIES),
                reraise=True,
            ):
                with attempt:
                    response = await self._send(url, attempt.retry_state.attempt_number)
        except _TransientStatusError as e:
            raise UpstreamError(e.status_code, url, f"Upstream error after retries. {e.reason}") from e
        except httpx2.TooManyRedirects as e:
            raise UpstreamError(404, url, "Not found") from e
        except httpx2.TimeoutException as e:
            raise UpstreamError(502, url, "Timeout") from e
        except httpx2.TransportError as e:
            raise UpstreamError(502, url, "Connection error") from e
        except httpx2.HTTPError as e:
            raise UpstreamError(500, url, f"Error. {e}") from e

        # AsyncRetrying always makes one attempt and re-raises the last failure, so only a broken contract gets here.
        if response is None:
            raise UpstreamError(500, url, "Error. No upstream attempt was made")

        # Every 5xx is retryable, so a status that reaches this point without raising is below 500.
        if response.status_code >= 400:
            raise UpstreamError(response.status_code, url, f"Client Error. {response.reason_phrase}")

        # An empty or whitespace-only body is never cached: it would otherwise be served for CACHE_TTL_SECONDS.
        if self._cache is not None and response.is_success and response.content.strip():
            self._cache.set(url, response.content, expire=self._config.CACHE_TTL_SECONDS)

        return UpstreamResponse(url=url, status_code=response.status_code, content=response.content)

    async def _send(self, url: str, attempt_number: int) -> httpx2.Response:
        async with self._semaphore:
            await self._throttle.wait()
            # Picking a User-Agent carries no security property, so a non-cryptographic PRNG is fine.
            user_agent = random.choice(USER_AGENTS)  # noqa: S311
            response = await self._http.get(url, headers={"User-Agent": user_agent})
        logger.debug("GET %s cache=miss status=%d attempt=%d", url, response.status_code, attempt_number)
        if _is_retryable_status(response.status_code):
            raise _TransientStatusError(
                response.status_code,
                response.reason_phrase,
                _parse_retry_after(response.headers.get("Retry-After")),
            )
        return response
