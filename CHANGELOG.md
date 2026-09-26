# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.1] - 2026-09-26

### Added

- `GET /competitions/{competition_id}/table`: a competition's league table, or one table per group (none for knockout
  cups), in a season (`season_id`, the current season by default). Each row has `clubId`, `clubName`, `position`,
  `previousPosition`, `played`, `won`, `drawn`, `lost`, `goalsFor`, `goalsAgainst`, `goalDifference`, `points`,
  `pointsDeducted` and `zone`.
- `GET /competitions/{competition_id}/fixtures`: a competition's games and results in a season (`season_id`, the
  current season by default), optionally for one `matchday`. Each game has its season, competition, matchday, `stage`
  (group or knockout round), UTC `date`, clubs, goals (after regular or extra time, without shootout goals),
  `endedAfter` (`regular`, `extra_time` or `shootout`; `null` until the game has finished), `shootout` (the penalty
  shootout result `{home, away}`, or `null`), status, `attendance` and match report `url`.
- Both are built from Transfermarkt's JSON API (`tmapi.transfermarkt.technology`), keep `null` values and return 404
  for an unknown competition.
- `GET /clubs/{club_id}/fixtures`: a club's games and results in all competitions in a season (`season_id`, the
  current season by default), sorted by date. Each game has the fields of a competition fixtures game plus `venue`
  (`home` or `away`) and `result` (`W`, `D` or `L`; `null` until the game has finished).
- `GET /clubs/{club_id}/squad`: a club's or national team's squad in a season (`season_id`, the current squad by
  default), with `isNationalTeam` and, per player, `shirtNumber`, `isCaptain`, `position`, `dateOfBirth`, `age`,
  `nationalities`, `height`, `foot`, `contractUntil`, `marketValue` and the squad entry `type`. Player details are
  the player's current ones, also in a past season's squad.
- Both club routes are built from Transfermarkt's JSON API, keep `null` values and return 404 for an unknown club.
- `GET /clubs/{club_id}/players` for a national team: players have `internationalMatches`, `internationalGoals` (0
  where the page shows `-`), `debut` and `status` (e.g. `Team captain` or an injury), and `currentClub` is the
  player's club; `seasonId` is set as for clubs.
- `GET /games/{game_id}`: a game's report, built from Transfermarkt's JSON API, with the fields of a fixtures game
  plus `stageLabel` (e.g. `QF 3`), `duration` (90 or 120 once finished), `stadium` (`{id, name, city}`), `referee`,
  `home` / `away` (`club`, `coach`, `formation`, `lineup` and `substitutes` with each player's `shirtNumber`,
  `isCaptain`, `position`, `marketValue` and `age` at the game, and tmapi's raw per-club `statistics`) and `events`
  in chronological order (goals, cards, substitutions, penalty shootout kicks, missed penalties and coach sanctions,
  each with its `club`, `action`, `reason`, `player`, `relatedPlayer` and `score`: the running score, or for a
  shootout kick the shootout tally). `isLive` and `isFinished` are derived from the report's score. It keeps `null`
  values and returns 404 for an unknown game.

### Fixed

- `GET /clubs/{club_id}/players` returned 502 ("Squad column 'name' has 0 values") for national teams, whose squad
  page has a different table. The table layout is now detected from its column headers; an unknown layout is a 502
  naming the headers. A page that is not the requested club's squad page (an unknown club redirects to "Most
  valuable clubs") is a 404.
- `GET /competitions/search/{competition_name}` returned no results for searches whose results page has only
  international competitions (e.g. `euro`, `world cup`, `nations`), and could give a result the next row's country
  when only some rows had one. International competitions have no country, so the country column had fewer values
  than the others; results are now read row by row and `country` is omitted for competitions without one.
- `GET /clubs/{club_id}/squad` returned 502 when tmapi sent `null` for a player's `position`, `preferredFoot`,
  `marketValueDetails` or `marketValueDetails.current`; `null` is now treated like a missing value.

## [4.0.0] - 2026-09-26

First release of this fork of [felipeall/transfermarkt-api](https://github.com/felipeall/transfermarkt-api) (3.0.0).
The HTTP layer, the parsers and the test suite were rebuilt; the notes below are for API consumers upgrading from 3.x.

### Breaking changes

- Python 3.12 or newer is required (was 3.9), and the project is managed with [uv](https://docs.astral.sh/uv/)
  instead of Poetry (`uv sync`, `uv run ...`); `poetry.lock` and `requirements.txt` are gone.
- The Fly.io configuration (`fly.toml`) and the Fly deploy workflow are removed; there is no hosted instance. The
  Docker image (`Dockerfile`) and `docker-compose.yml` are the deployable artifacts.
- Inputs are validated before anything is requested from Transfermarkt, and invalid input is a 422:
  `player_id` and `club_id` must be digits (at most 12), `competition_id` 1 to 12 letters or digits, `season_id` four
  digits and `page_number` an integer of at least 1.
- Every error response has a `detail` key: `{"detail": "<message>"}` for 404, 429, 500 and upstream errors (502 and
  passed-through upstream statuses); 422 keeps FastAPI's `{"detail": [...]}` list. Rate-limit errors (429) used
  slowapi's `{"error": ...}` body before. An unexpected error is logged and returned as 500
  `{"detail": "Internal server error"}` without internals.
- Dates are parsed day-first, as Transfermarkt serves them (`dd/mm/yyyy`). Every date whose day was 12 or less (and
  differed from the month) was previously swapped (read month-first), so many values change: birth dates, joined and contract dates, transfer,
  injury and market-value dates.
- `/players/{player_id}/stats` is rebuilt on Transfermarkt's JSON API (`tmapi.transfermarkt.technology`) and returns
  one row per season, competition and club the player was fielded for, sorted newest season first. A row has
  `seasonId`, `seasonName`, `competitionId`, `competitionName`, `clubId`, `clubName`, `appearances`, `goals`,
  `assists`, `ownGoals`, `penaltyGoals`, `yellowCards`, `secondYellowCards`, `redCards`, `minutesPlayed`,
  `goalsConceded` and `cleanSheets` (`seasonName`, `clubName`, `ownGoals`, `penaltyGoals`, `secondYellowCards`,
  `goalsConceded` and `cleanSheets` are new). `goalsConceded` and `cleanSheets` are `null` for rows without goalkeeper
  games, and `null` values are kept in this response. A new `season_id` query parameter limits the rows to one season.
  An unknown player returns 404.
- `/clubs/{club_id}/players`: the `joined` field is removed (it was always null); `seasonId` (the season of the
  returned squad) and `signedFromFee` (integer or `null`) are added.
- `/clubs/{club_id}/profile`: `stadiumName`, `stadiumSeats`, `currentTransferRecord` and `squad.nationalTeamPlayers`
  are optional, and `fifaWorldRanking` is an integer or `null` (was a string).
- Search terms are URL-encoded when they are sent to Transfermarkt; a query such as `a&b` is no longer truncated.
- All routes omit `null` fields (except `/players/{player_id}/stats`, see above); the competition endpoints returned
  them before, and `/clubs/{club_id}/profile` and `/clubs/{club_id}/players` omitted fields equal to their defaults.
  Empty `/clubs/{club_id}/players` fields are omitted instead of returned as `""`.
- `updatedAt` is in UTC.

### Added

- `GET /health`, returning `{"status": "ok", "version": "<app version>"}`; it is not rate limited. The app reports
  its version, and every route has an OpenAPI summary and description.
- Request logging: one line per request at `INFO`, upstream fetches (with cache hit/miss) at `DEBUG` and upstream
  retries at `WARNING`, at the level set by `LOG_LEVEL`. Uvicorn's access log is disabled to avoid duplicates.
- `CORS_ORIGINS`: comma-separated browser origins allowed to call the API (empty disables CORS).
- Rate limiting behind a reverse proxy: uvicorn trusts `X-Forwarded-For` from the addresses in
  `FORWARDED_ALLOW_IPS`, so the inbound limiter keys on the client address instead of the proxy's.
- Upstream rate-limit avoidance for requests to Transfermarkt: a process-wide minimum interval, bounded concurrency,
  a timeout, retries with backoff on 408/425/429/5xx and connection errors (honouring `Retry-After`), a rotating
  User-Agent and a disk-backed TTL cache of successful responses. Settings: `CACHE_ENABLE`, `CACHE_DIR`,
  `CACHE_TTL_SECONDS`, `CACHE_SIZE_LIMIT_MB`, `OUTBOUND_MIN_INTERVAL_MS`, `OUTBOUND_MAX_RETRIES`,
  `OUTBOUND_MAX_CONCURRENCY` and `OUTBOUND_TIMEOUT_S`.
- `HOST`, `PORT` and `RELOAD` settings for `python -m app.main` (it used hard-coded auto-reload).
- `docker-compose.yml`, which keeps the response cache in a named volume across restarts.
- Tests: offline snapshot tests that replay recorded Transfermarkt responses (`tests/fixtures`) through the real HTTP
  client and compare every endpoint's output to `tests/snapshots`; the fixture recorder
  `scripts/record_fixtures.py`; a live smoke suite (`tests/live`) that checks stable facts against the live site, and
  a nightly GitHub Actions workflow that runs it.
- CI on GitHub Actions: ruff (lint and format), mypy, pytest, pip-audit and a Docker build.

### Changed

- The HTTP layer is an async `httpx2` client shared by all endpoints, and pages are parsed once with `lxml`
  (`requests` and BeautifulSoup are no longer used). Endpoint output was unchanged by this refactor.
- Runtime dependencies are upgraded to current releases.
- The Docker image is a multi-stage build on `python:3.13-slim` with uv, runs as a non-root user and without
  auto-reload.

### Fixed

- Dates were parsed month-first, swapping day and month in every ambiguous date (see Breaking changes).
- Player profile `dateOfBirth` and `age` were always `null` (the parser expected the old `Jun 24, 1987 (36)` format).
- Thousands separators were read as decimal points (club `members` 170.000 became 170), and float arithmetic
  truncated amounts (€16.58m became 16579999).
- Club profile `league.countryId` kept only the first digit of the country id (157 became `"1"`).
- Club profile `fifaWorldRanking` and `confederation` were never found (the page labels changed).
- Club profiles of national teams returned 500 because stadium, transfer-record and national-team-player fields are
  absent there.
- `/players/{player_id}/stats` returned an empty list for every player, because Transfermarkt no longer renders the
  detailed stats table in the page HTML.
- `/clubs/{club_id}/players`: `signedFrom` sometimes held the fee label (`": Ablöse €86.00m"`) instead of the club
  name; `seasonId` was always `null`; in past seasons a height of `N/A` shifted every following player's height and
  dropped the last players; a squad table whose columns do not line up now fails with 502 instead of returning
  misaligned players.
- An unexpected tmapi response for `/players/{player_id}/stats` fails with 502 instead of returning wrong numbers.
- Upstream connection errors and timeouts return 502 (was 500), and transient upstream 429/5xx responses are retried
  instead of being passed straight to the client.
- A path-shaped id could make the API request arbitrary Transfermarkt paths; ids are now validated.
- Squad and competition URLs without a season no longer contain `None`.
- The Docker image crashed on import (stale `requirements.txt` without `diskcache` and `tenacity`, and a Python 3.9
  base image).
