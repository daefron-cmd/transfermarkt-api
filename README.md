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

# Instantiate a Poetry virtual env
$ poetry shell

# Install the dependencies
$ poetry install --no-root

# (optional) Append the current directory to PYTHONPATH
$ export PYTHONPATH=$PYTHONPATH:$(pwd)

# Start the API server
$ python app/main.py

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

### Upstream Rate-Limit Avoidance

Outbound requests to Transfermarkt go through a shared `requests.Session` with
connection reuse, a rotating User-Agent pool, a process-wide minimum-interval
throttle, exponential-backoff retry on 408/425/429/5xx (via `tenacity`), and a
disk-backed TTL response cache (via `diskcache`). The cache lives at
`CACHE_DIR` (default `.cache/http/`, gitignored) and is keyed by URL.

### Environment Variables

| Variable                   | Description                                                                                            | Default        |
|----------------------------|--------------------------------------------------------------------------------------------------------|----------------|
| `RATE_LIMITING_ENABLE`     | Enable inbound rate limiting for clients calling this API                                              | `false`        |
| `RATE_LIMITING_FREQUENCY`  | Delay allowed between each inbound API call. See [slowapi](https://slowapi.readthedocs.io/en/latest/) | `2/3seconds`   |
| `CACHE_ENABLE`             | Enable disk cache of upstream Transfermarkt responses                                                  | `true`         |
| `CACHE_DIR`                | Directory for the disk cache                                                                           | `.cache/http`  |
| `CACHE_TTL_SECONDS`        | TTL for cached upstream responses                                                                      | `3600`         |
| `CACHE_SIZE_LIMIT_MB`      | Maximum cache size on disk                                                                             | `500`          |
| `OUTBOUND_MIN_INTERVAL_MS` | Minimum gap between outbound requests to Transfermarkt (per process)                                   | `500`          |
| `OUTBOUND_MAX_RETRIES`     | Retry attempts on transient upstream errors (429, 5xx, etc.)                                           | `4`            |

> **Python version**: this fork requires Python `>=3.10` (bumped from upstream's `^3.9` because `tenacity` dropped 3.9 support).
