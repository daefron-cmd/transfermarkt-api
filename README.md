# transfermarkt-api

This project provides a lightweight and easy-to-use interface for extracting data from [Transfermarkt](https://www.transfermarkt.com/) 
by applying web scraping processes and offering a RESTful API service via FastAPI. With this service, developers can 
seamlessly integrate Transfermarkt data into their applications, websites, or data analysis pipelines.

This is a maintained fork of [felipeall/transfermarkt-api](https://github.com/felipeall/transfermarkt-api) with a
rebuilt HTTP layer, parsers and tests.

Interactive API docs are served at http://localhost:8000/ (redirects to `/docs`) once the server runs.

### Running Locally

````bash
# Clone the repository
$ git clone https://github.com/daefron-cmd/transfermarkt-api.git

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
$ git clone https://github.com/daefron-cmd/transfermarkt-api.git

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

### Endpoints

Every route is a `GET`; `/docs` has the full request and response schemas.

| Route                                     | Returns                                                                          |
|-------------------------------------------|----------------------------------------------------------------------------------|
| `/players/search/{player_name}`           | One page of players matching a name (`page_number`)                              |
| `/players/{player_id}/profile`            | A player's profile: personal details, position, club, contract, value            |
| `/players/{player_id}/market_value`       | A player's current market value, its history and rankings                        |
| `/players/{player_id}/transfers`          | A player's transfer history with clubs, dates and fees                           |
| `/players/{player_id}/jersey_numbers`     | The jersey numbers a player has worn per season and club                         |
| `/players/{player_id}/stats`              | A player's stats per season, competition and club (`season_id`); tmapi           |
| `/players/{player_id}/injuries`           | One page of a player's injury history (`page_number`)                            |
| `/players/{player_id}/achievements`       | The titles and awards a player has won                                           |
| `/clubs/search/{club_name}`               | One page of clubs matching a name (`page_number`)                                |
| `/clubs/{club_id}/profile`                | A club's profile: stadium, league, squad summary, market value                   |
| `/clubs/{club_id}/players`                | A club's or national team's squad in a season (`season_id`)                      |
| `/clubs/{club_id}/fixtures`               | A club's games and results in all competitions in a season (`season_id`); tmapi  |
| `/clubs/{club_id}/squad`                  | A club's or national team's squad in a season (`season_id`); tmapi               |
| `/competitions/search/{competition_name}` | One page of competitions matching a name (`page_number`)                         |
| `/competitions/{competition_id}/clubs`    | The clubs in a competition in a season (`season_id`)                             |
| `/competitions/{competition_id}/table`    | A competition's league table, or one per group, in a season (`season_id`); tmapi |
| `/competitions/{competition_id}/fixtures` | A competition's games and results in a season (`season_id`, `matchday`); tmapi   |
| `/games/{game_id}`                        | A game's report: result, lineups, events, statistics, stadium, referee; tmapi    |
| `/health`                                 | Service status and version                                                       |

Routes marked tmapi are built from Transfermarkt's JSON API (see [tmapi Data Source](#tmapi-data-source)); the
others parse the site's HTML pages.

National teams are clubs on Transfermarkt: find their ids with `/clubs/search/{club_name}` (e.g. Spain is `3375`)
and use them with every `/clubs/{club_id}/...` route. Their competitions are competitions like any other (e.g. `EURO`,
`UNLA`); international competitions have no `country` in `/competitions/search/{competition_name}` results.

`/clubs/{club_id}/players` returns the squad table of the club's squad page, so its player fields depend on the page:

- a club, current season: `id`, `name`, `position`, `dateOfBirth`, `age`, `nationality`, `height`, `foot`,
  `joinedOn`, `signedFrom`, `signedFromFee`, `contract`, `marketValue`, `status`;
- a club, past season: the same without `contract`, plus `currentClub` (the player's club today);
- a national team: `id`, `name`, `position`, `dateOfBirth`, `age`, `currentClub` (the player's club, or e.g.
  `Retired`), `height`, `foot`, `internationalMatches` and `internationalGoals` (0 where the page shows `-`), `debut`
  (date of the first international match), `marketValue` and `status`.

A squad table with other columns returns 502 naming its headers.

### Health, Errors and Validation

`GET /health` returns `{"status": "ok", "version": "<app version>"}` and is not rate limited.

Every error response is JSON of the form `{"detail": "<message>"}`, except input validation errors (422), which keep
FastAPI's format (`{"detail": [{"loc": ..., "msg": ..., "type": ...}]}`). An unexpected error is logged with its
traceback and returned as 500 `{"detail": "Internal server error"}`; exceeding the inbound rate limit is a 429. An
empty or unparseable upstream page is a 502 whose `detail` names the upstream URL (`Empty upstream response (0 bytes)
for url: ...`); an empty body is never cached.

Inputs are validated before anything is requested from Transfermarkt (bad input is a 422, documented in `/docs`):

- `player_id`, `club_id`: digits only, at most 12;
- `competition_id`: 1 to 12 letters or digits (e.g. `GB1`);
- `season_id` (query parameter): four digits, the season's starting year (`2024` = `24/25`); omitted = current season
  (for `/players/{player_id}/stats`: all seasons);
- `page_number`: an integer, at least 1;
- `matchday` (query parameter of `/competitions/{competition_id}/fixtures`): an integer, at least 1;
- search terms: any text; it is URL-encoded when it is sent to Transfermarkt.

### Logging

The app logs to stderr through the standard `logging` module, one line per record
(`<timestamp> <level> <logger> <message>`), at `LOG_LEVEL`. Every handled request is logged at `INFO`
(`GET /players/28003/profile 200 412.3ms`, logger `app.requests`); every upstream fetch at `DEBUG`
(URL, cache hit/miss, status, attempt; logger `app.http`) and every upstream retry at `WARNING` with its reason.
Uvicorn's own access log is disabled at startup so requests are not logged twice.

### Behind a Reverse Proxy

The inbound rate limiter keys on the client address. Behind a reverse proxy that is the proxy's address unless
uvicorn trusts the proxy's `X-Forwarded-For` header. The Docker image runs uvicorn with `--proxy-headers`, and
`python -m app.main` enables proxy headers too; both trust only the addresses in `FORWARDED_ALLOW_IPS`
(default `127.0.0.1`). Set it to the proxy's IP address (comma-separated for several), or to `*` if only the proxy can
reach the app, e.g. in `.env` or under `environment:` in `docker-compose.yml`. Do not use `*` when clients can reach the
app directly: they could then choose their own rate-limit key.

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

### tmapi Data Source

`/players/{player_id}/stats`, `/clubs/{club_id}/fixtures`, `/clubs/{club_id}/squad`,
`/competitions/{competition_id}/table`, `/competitions/{competition_id}/fixtures` and `/games/{game_id}` are built
from `tmapi.transfermarkt.technology`, the JSON API behind the site's own web components (Transfermarkt no longer
renders the detailed stats table in the page HTML).
**tmapi is unofficial and undocumented**: it may change or disappear without notice. Its responses are
validated (`app/tmapi.py`) and an unexpected shape returns 502 instead of wrong data. Requests go through
the same `TransfermarktClient` (throttle, retries, disk cache); the `/attributes` table is fetched once per process.
Club names (and the player stats' and club fixtures' competition names, the club squad's players and the game
report's players, coaches and referee) come from its batch lookups (`/clubs?ids[]=...`, `/competitions?ids[]=...`,
`/players?ids[]=...`, `/coaches?ids[]=...`, `/referees?ids[]=...`, sorted ids, 50 per request).

#### Player stats

The endpoint fetches the player's per-game rows (`/player/{id}/performance-game`) and the names of their
competitions and clubs, then returns one entry per season, competition and club the player was fielded for (a
national team for international games). `?season_id=2024` limits it to one season. Only games with participation state
`played` count:

- `appearances`: played games; `minutesPlayed`, `goals`, `assists`, `ownGoals`, `penaltyGoals`: sums over them;
- `yellowCards`: yellows that did not become a second yellow; `secondYellowCards`, `redCards` (straight red):
  games with that card;
- `goalsConceded` / `cleanSheets`: goals conceded while on the pitch / games the team kept a clean sheet
  (whole-match score), over games played in a goalkeeper position; a game without a recorded position counts
  when the player's main position (`/player/{id}`) is goalkeeper. `null` when the entry has no such games.

Entries are sorted by season (newest first), then competition name. Season ids are the starting year
(`2024` = `24/25`); some national-team competitions (qualifiers, Nations League Finals) use calendar-year
seasons, so season `2024` also holds such games of 2025 (`seasonName` `"2025"`). An unknown player returns 404.

#### Competition table and fixtures

Both fetch the competition (`/competition/{id}`; an unknown competition returns 404), whose name and current season
are used for `name` and, when `season_id` is omitted, `seasonId`.

- `/table` (`/competition/{id}/table`) returns `tables`: one for a league, one per group for a competition with a
  group stage (`Group A`, ...), none (`[]`) for a knockout cup. Each has a `name` and `rows` sorted by `position`
  (`clubId`, `clubName`, `position`, `previousPosition`, `played`, `won`, `drawn`, `lost`, `goalsFor`,
  `goalsAgainst`, `goalDifference`, `points`, `pointsDeducted`, `zone`: the table zone such as `Relegated`, or
  `null`).
- `/fixtures` (`/competition/{id}/fixtures`) returns `games` sorted by matchday, date and id; `?matchday=N` keeps one
  matchday (no games is an empty list). A game has `id`, `seasonId`, `seasonName`, `competitionId`,
  `competitionName`, `matchday`, `stage` (`Group A`, `Round of 16`, ...; `null` for plain league rounds), `date`
  (UTC), `isTimeDefined` (tmapi's flag for whether the kick-off time is set), `homeClub` / `awayClub`
  (`{id, name}`), `homeGoals` / `awayGoals` (`null` before kick-off; the score after regular or extra time, without
  shootout goals), `endedAfter` (`regular`, `extra_time` or `shootout`, `null` until the game has finished; any other
  upstream value is passed through), `shootout` (`{home, away}`, the penalty shootout result, `null` unless
  `endedAfter` is `shootout`), `isFinished`, `isLive`, `attendance` and `url` (the Transfermarkt match report path).
  tmapi's fixtures score of a shootout game includes the shootout goals (1-1 and 5-3 on penalties is 6-4), so for
  each finished shootout game the game report (`/game/{id}`, one extra request per game) is fetched and the score
  after regular or extra time is taken from its last goal.

`null` values are kept in these responses.

#### Club fixtures and squad

Both fetch the club (`/club/{id}`; an unknown club returns 404), whose name is used for `name`.

- `/fixtures` (`/club/{id}/fixtures`) returns the club's `games` in all competitions (league, cups, European and
  international competitions, friendlies), sorted by date and id. A game has the fields of a competition fixtures
  game (see above; the competition names come from the `/competitions` lookup) plus `venue` (`home` or `away`) and
  `result` (`W`, `D` or `L` from the club's point of view, tmapi's own value: after a shootout the shootout winner
  has `W`; `null` until the game has finished). `seasonId` is the requested season or, when `season_id` is omitted,
  the season shared by all returned games (`null` if they have none in common or there are none). For national teams
  tmapi's season is a calendar year, so `?season_id=2024` holds Euro 2024 games whose own `seasonId` is `2023`.
- `/squad` (`/club/{id}/squad`) returns `isNationalTeam`, `seasonId` (the requested season; `null` for the current
  squad, whose season tmapi does not state) and `players` in tmapi's squad order (an empty list when tmapi has no
  squad for the season). A player has `id`, `name`, `shirtNumber`, `isCaptain`, `position`, `dateOfBirth`, `age`,
  `nationalities` (country names; one or two), `height` (centimetres), `foot`, `contractUntil`, `marketValue` (`null`
  when tmapi has none or 0, as for retired players) and `type` (tmapi's squad entry type: `current`, `nationalTeam`,
  `historical`, ...). The player details come from the `/players` lookup and are the player's current ones even in a
  past season's squad: `age`, `position`, `contractUntil` and `marketValue` are today's, not the season's (the
  `/clubs/{club_id}/players` HTML route shows a past squad's ages and market values at the time).

`null` values are kept in these responses.

#### Game report

`/games/{game_id}` fetches the game report (`/game/{id}`; an unknown game returns 404), then the `/attributes`
tables, the names of its clubs, players and coaches, and, when the report has them, its referee's name and its stadium
(`/stadium/{id}`). Game ids are in the fixtures' `id` and `url`. The response has the fields of a fixtures game (see
above; `competitionName` is the report's) plus:

- `stageLabel`: tmapi's tournament stage label such as `QF 3`, `null` for other games;
- `duration`: the minutes played (90, or 120 after extra time) once the game has finished, else `null`;
- `stadium` (`{id, name, city}`) and `referee` (`{id, name}`), `null` when the report has none (e.g. before
  kick-off); `stadium` is also `null` when tmapi has no stadium for the report's stadium id;
- `home` / `away`: `club` (`{id, name}`), `coach` (`{id, name}`: the game's coach, or before kick-off the club's
  head coach; `null` when there is none), `formation` (e.g. `3-4-2-1`), `lineup` and `substitutes` (each player's
  `id`, `name`, `shirtNumber`, `isCaptain`, `position` (`null` when tmapi has none, as for many substitutes),
  `marketValue` and `age` at the time of the game) and `statistics`, tmapi's raw per-club statistics (passing, goals,
  defence, set pieces, ...) passed through unchanged, `null` when the report has none. Before kick-off the lists are
  empty and `formation` is `null`.
- `events` in chronological order (by minute and added time): `type` (`goal`, `card`, `substitution`, `shootout`,
  `missed_penalty`, `coach_sanction`; any other upstream type is passed through in lower case), `minute`,
  `addedTime`, `club`, `action` (e.g. `Left-footed shot`, `Yellow card`, `Saved`) and `reason` (e.g. `Pass`, `Foul`,
  `Tactical`) from the `/attributes` tables (`null` when unknown or not given), `player`, `relatedPlayer` and `score`
  (the running score after a goal or scored shootout kick, else `null`). `player` is the scorer, the carded player,
  the shootout taker or, in a substitution, the player going off; `relatedPlayer` is the assist (the fouled player
  for a won penalty) or, in a substitution, the player coming on. A coach sanction has no player. A shootout kick's
  `score` is the shootout tally, like `shootout` (tmapi's includes the goals of the game, which are subtracted: in a
  1-1 game won 5-3 on penalties the first scored kick is `{home: 1, away: 0}` and the last `{home: 5, away: 3}`).

The report does not state whether the game is live or finished, so `isLive` is derived from its score
(`score.details.gameEndType` is `Playing`) and `isFinished` is set when the game has a score and is not live; this
matched the fixture lists' flags on every game checked, but a live game has only been seen in its second half.
`null` values are kept in this response.

### Environment Variables

| Variable                   | Description                                                                                            | Default        |
|----------------------------|--------------------------------------------------------------------------------------------------------|----------------|
| `HOST`                     | Bind address used by `python -m app.main` (the Docker image always binds `0.0.0.0`)                    | `127.0.0.1`    |
| `PORT`                     | Port used by `python -m app.main`                                                                      | `8000`         |
| `RELOAD`                   | Enable uvicorn auto-reload when running `python -m app.main`                                           | `false`        |
| `LOG_LEVEL`                | Log level of the app's logs (`DEBUG`, `INFO`, `WARNING`, ...)                                          | `INFO`         |
| `CORS_ORIGINS`             | Comma-separated browser origins allowed to call the API (CORS); empty disables CORS                    | *(empty)*      |
| `FORWARDED_ALLOW_IPS`      | Proxy addresses trusted to set `X-Forwarded-For` (`*` = any); read by uvicorn, see above               | `127.0.0.1`    |
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
$ uv run pyright
$ uv run pytest -q               # offline suite: unit tests + fixture-backed endpoint snapshots
````

#### Quality gates

The repo carries the pawl quality stack (`mech/`). A pre-commit gate
(`.githooks/pre-commit` and `.githooks/pre-merge-commit`) runs ruff, pyright, a Pawl compatibility-record check and
the offline suite, and blocks the commit on any failure. Git hook paths are local config, so each clone is unarmed
until it runs:

````bash
$ git config core.hooksPath .githooks
$ uv run python -m mech.validation   # exit 0 once armed
````

On GitHub, `.github/workflows/push.yml` re-runs the same checks on every pushed branch tip and adds coverage
reachability and diff-scoped mutation testing (`MUTANTS.md` is the accepted-survivor baseline);
`.github/workflows/weekly.yml` runs the full mutation catalog, pip-audit and an issue handoff on the first day of each
month. `.github/workflows/ci.yml` keeps the dependency audit and the Docker build. Conventions for working under the
gate are in [CLAUDE.md](CLAUDE.md); departures from stock pawl are recorded in [DEVIATIONS.md](DEVIATIONS.md).

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

**Live smoke tests.** `tests/live/` calls every endpoint against the live Transfermarkt site, with the disk
cache off and the normal outbound throttle, to catch HTML and JSON changes the recorded fixtures cannot show. It
asserts only stable facts (ids, names, birth dates, past transfers and squads) and structural invariants (ISO dates,
numeric ids, integer fees and heights, season ids), never volatile values such as market values. The pytest
`addopts` deselect it (`-m "not live"`), so `uv run pytest` never runs it; the last `-m` wins, so run it with:

````bash
$ uv run pytest tests/live -m live --force-enable-socket -q
````

The full suite is a local run. The nightly GitHub Actions workflow `.github/workflows/live.yml` (`Live smoke`) runs
only the cases marked `tmapi`, whose endpoints fetch nothing from www.transfermarkt.com, only from
`tmapi.transfermarkt.technology`: transfermarkt.com answers GitHub-hosted runners with HTTP 202 and an empty body for
every page (verified 2026-09-26), so the HTML-backed cases fail there with 502. To run the same selection locally:

````bash
$ uv run pytest tests/live -m "live and tmapi" --force-enable-socket -q
````

The workflow runs every day at 04:00 UTC and can be started by hand (`workflow_dispatch`). It first prints an upstream
reachability check (status, size, content type and the first bytes of a player page and a tmapi response), which shows
whether transfermarkt.com still blocks the runners, and runs the tests with `LOG_LEVEL=DEBUG` and `-rA`, which puts
every upstream fetch and its status in the log.

### Changelog

See [CHANGELOG.md](CHANGELOG.md) for the changes in each release.
