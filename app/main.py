import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse, RedirectResponse

from app import __version__
from app.api.api import api_router
from app.http import TransfermarktClient, UpstreamError
from app.settings import settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_HANDLER_NAME = "transfermarkt-api"

DESCRIPTION = (
    "An unofficial API that scrapes public pages of [Transfermarkt](https://www.transfermarkt.com/) and returns "
    "players, clubs and competitions as JSON. It is not affiliated with Transfermarkt, and the data belongs to "
    "Transfermarkt. Be polite: keep request rates low, rely on the cache and respect the rate limits."
)

logger = logging.getLogger("app.requests")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMITING_FREQUENCY],
    enabled=settings.RATE_LIMITING_ENABLE,
)


def configure_logging(level: str) -> None:
    """Log to stderr as single lines at `level` (idempotent). Uvicorn's access log is replaced by `log_requests`."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(handler.get_name() == LOG_HANDLER_NAME for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.set_name(LOG_HANDLER_NAME)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    logging.getLogger("uvicorn.access").disabled = True
    # httpx2 logs every request at INFO; TransfermarktClient logs its upstream fetches itself (at DEBUG).
    for name in ("httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.LOG_LEVEL)
    async with TransfermarktClient() as client:
        app.state.client = client
        yield


app = FastAPI(title="Transfermarkt API", version=__version__, description=DESCRIPTION, lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.include_router(api_router)


# SlowAPIMiddleware only calls a synchronous handler; for a coroutine it falls back to slowapi's {"error": ...} body.
@app.exception_handler(RateLimitExceeded)
def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    response = JSONResponse(status_code=429, content={"detail": f"Rate limit exceeded: {exc.detail}"})
    return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)


@app.exception_handler(UpstreamError)
async def upstream_error_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.middleware("http")
async def log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """
    Log one line per request, and turn an unhandled exception into a generic 500 after logging its traceback. The
    exception is handled here rather than re-raised to Starlette's ServerErrorMiddleware, so uvicorn does not log it
    a second time.
    """
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error in %s %s", request.method, request.url.path)
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s %d %.1fms", request.method, request.url.path, response.status_code, duration_ms)
    return response


cors_origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
if cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_methods=["GET"], allow_headers=["*"])


@app.get("/", include_in_schema=False)
def docs_redirect():
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    tags=["health"],
    summary="Health check",
    description="Report that the service is running and its version; this route is not rate limited.",
)
@limiter.exempt
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        proxy_headers=True,
        forwarded_allow_ips=settings.FORWARDED_ALLOW_IPS,
    )
