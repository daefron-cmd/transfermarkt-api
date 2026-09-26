# transfermarkt-api

This project provides a lightweight and easy-to-use interface for extracting data from [Transfermarkt](https://www.transfermarkt.com/) 
by applying web scraping processes and offering a RESTful API service via FastAPI. With this service, developers can 
seamlessly integrate Transfermarkt data into their applications, websites, or data analysis pipelines.

Please note that the deployed application is used only for testing purposes and has a rate limiting 
feature enabled. If you'd like to customize it, consider hosting in your own cloud service. 

### API Swagger
https://transfermarkt-api.fly.dev/

### Running Locally

````bash
# Clone the repository
$ git clone https://github.com/felipeall/transfermarkt-api.git

# Go to the project's root folder
$ cd transfermarkt-api

# Install Python and the dependencies (requires uv: https://docs.astral.sh/uv/)
$ uv sync

# Start the API server with auto-reload
$ uv run uvicorn app.main:app --reload

# ...or start it using HOST / PORT / RELOAD from the environment or .env
$ uv run python -m app.main

# Access the API local page
$ open http://localhost:8000/
````

### Running via Docker

````bash
# Clone the repository
$ git clone https://github.com/felipeall/transfermarkt-api.git

# Go to the project's root folder
$ cd transfermarkt-api

# Build the Docker image
$ docker build -t transfermarkt-api . 

# Instantiate the Docker container
$ docker run -d -p 8000:8000 transfermarkt-api

# Access the API local page
$ open http://localhost:8000/
````

Or with Docker Compose, which builds the image, loads `.env` if present and keeps the upstream
response cache in a named volume across restarts:

````bash
$ docker compose up -d --build
````

### Upstream Rate-Limit Avoidance

The endpoints are `async` and share one `TransfermarktClient` (`app/http.py`), an async
`httpx2` client created at startup and closed at shutdown. Every outbound request to Transfermarkt:

- reuses pooled connections, follows redirects and sends a User-Agent picked at random per request;
- is spaced by a process-wide minimum interval (`OUTBOUND_MIN_INTERVAL_MS`, enforced with an
  `asyncio.Lock`, never blocking the event loop) and capped at `OUTBOUND_MAX_CONCURRENCY` in flight;
- times out after `OUTBOUND_TIMEOUT_S` seconds;
- is retried (via `tenacity`) on 408/425/429/5xx and on connection errors, timeouts and protocol errors,
  up to `OUTBOUND_MAX_RETRIES` attempts, with exponential backoff and jitter; a numeric `Retry-After`
  header is honoured instead of the backoff (capped at 60 seconds);
- is served from a disk-backed TTL cache (via `diskcache`) when possible. Only successful (2xx)
  bodies are stored, for `CACHE_TTL_SECONDS`, keyed by the normalised URL; errors are never cached.
  The cache lives at `CACHE_DIR` (default `.cache/http/`, gitignored).

Upstream failures are returned as `{"detail": "..."}` JSON: upstream 4xx statuses (and 5xx after
retries) pass through, too many redirects become 404, and connection errors or timeouts become 502.

### Environment Variables

| Variable                   | Description                                                                                            | Default        |
|----------------------------|--------------------------------------------------------------------------------------------------------|----------------|
| `HOST`                     | Bind address used by `python -m app.main`                                                              | `0.0.0.0`      |
| `PORT`                     | Port used by `python -m app.main`                                                                      | `8000`         |
| `RELOAD`                   | Enable uvicorn auto-reload when running `python -m app.main`                                           | `false`        |
| `RATE_LIMITING_ENABLE`     | Enable inbound rate limiting for clients calling this API                                              | `false`        |
| `RATE_LIMITING_FREQUENCY`  | Delay allowed between each inbound API call. See [slowapi](https://slowapi.readthedocs.io/en/latest/) | `2/3seconds`   |
| `CACHE_ENABLE`             | Enable disk cache of upstream Transfermarkt responses                                                  | `true`         |
| `CACHE_DIR`                | Directory for the disk cache                                                                           | `.cache/http`  |
| `CACHE_TTL_SECONDS`        | TTL for cached upstream responses                                                                      | `3600`         |
| `CACHE_SIZE_LIMIT_MB`      | Maximum cache size on disk                                                                             | `500`          |
| `OUTBOUND_MIN_INTERVAL_MS` | Minimum gap between outbound requests to Transfermarkt (per process)                                   | `500`          |
| `OUTBOUND_MAX_RETRIES`     | Retry attempts on transient upstream errors (429, 5xx, etc.)                                           | `4`            |
| `OUTBOUND_MAX_CONCURRENCY` | Maximum concurrent outbound requests to Transfermarkt (per process)                                    | `4`            |
| `OUTBOUND_TIMEOUT_S`       | Timeout in seconds for each outbound request                                                           | `30`           |

> **Python version**: this fork requires Python `3.12+` (`.python-version` pins 3.13 for development and Docker).

### Development

````bash
$ uv run ruff check . && uv run ruff format --check .
$ uv run mypy app
$ uv run pytest -q               # offline suite: unit tests + fixture-backed endpoint snapshots
````

#### Tests and fixtures

`uv run pytest` is fully offline: network sockets are disabled via `pytest-socket` in the pytest
`addopts`. `tests/endpoints/` calls every case in `tests/endpoints/cases.py` through the FastAPI
`TestClient`, serves upstream requests from `tests/fixtures/` (raw responses listed in
`tests/fixtures/index.json`) through the real `TransfermarktClient` with an `httpx2.MockTransport`
and compares the status code and JSON body (without `updatedAt`) to `tests/snapshots/<case>.json`. A test that needs an unrecorded upstream URL fails and names it.
Tests that must hit the live site go in `tests/live/` (marked `live`; run them with `--force-enable-socket`).

````bash
$ uv run python scripts/record_fixtures.py               # re-record all cases from the live site (throttled)
$ uv run python scripts/record_fixtures.py --case NAME   # re-record selected cases only
$ uv run pytest tests/endpoints --snapshot-update        # rewrite snapshots from the current output
````

The recorder wraps `TransfermarktClient.get`, so it goes through the normal request path and
responses still in the disk cache (`CACHE_DIR`) are recorded from the cache; set
`CACHE_ENABLE=false` to force fresh fetches.
