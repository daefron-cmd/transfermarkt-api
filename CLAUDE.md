# transfermarkt-api — project instructions

## Stakes
This is a solo fork of a Transfermarkt scraper: no customers, no SLA, and the only
person downstream of a bad commit is the maintainer next week. The quality stack
below (pawl, copied into `mech/`) is here because it is cheap, not because the
stakes are high: the offline suite runs in about 2 s (403 tests) and the whole
fail-closed commit gate in about 5 s, so blocking every commit on it costs
nothing worth arguing about. Mutation testing lives in push CI, where its minutes
do not land on each commit.

## Repo conventions
- `uv run` for everything (`uv run pytest`, `uv run ruff`, `uv run pyright`,
  `uv run python ...`). Never a bare tool from outside the venv.
- The offline suite (`uv run pytest -q`) serves upstream responses from
  `tests/fixtures/` and compares against `tests/snapshots/`. Re-record fixtures
  with `uv run python scripts/record_fixtures.py [--case NAME]` (hits the live
  site, throttled), then rewrite snapshots with
  `uv run pytest tests/endpoints --snapshot-update`. Review snapshot diffs; they
  are the endpoint contract.
- The live suite is deselected by the pytest `addopts` (`-m "not live"`, sockets
  disabled). Run it locally with
  `uv run pytest tests/live -m live --force-enable-socket -q` (the last `-m` wins).
- The nightly `live.yml` workflow runs only the `tmapi`-marked live cases:
  transfermarkt.com answers GitHub-hosted runners with an empty 202, while
  tmapi.transfermarkt.technology does not.

## Gates
- Tier 1 (PostToolUse): ruff check --fix, then format, then residuals on the
  touched file. Feedback only. This is a **user-level** hook
  (`~/.claude/hooks/ruff_after_edit.py`), not part of this repo — on a machine
  without it there is no per-edit feedback and the commit gate is the first
  thing that catches a lint error.
- Stop hook: declined (see DEVIATIONS.md)
- Commit gate (`.githooks/pre-commit` and `.githooks/pre-merge-commit` →
  `mech.commit_gate`): ruff, pyright(standard), a lightweight deployment
  compatibility-record check, then pytest with fixed seed `20260901`.
  Fail-closed, 600 s watchdog. The pytest step inherits this repo's `addopts`,
  so it runs the offline suite only; `tests/live` is never gated (see
  DEVIATIONS.md).
  `ruff check` runs `--no-cache`: ruff
  keys its cache on (mtime, permission mode) alone, so an isort verdict — which
  depends on whether *other* files exist — outlives the change that falsifies
  it. Keep the flag. Steps run against the working tree;
  if it differs from the index the gate names the diverging paths and proceeds,
  so partial staging is allowed and what the gate verified is a superset of
  what you committed. Push CI closes that gap.
- CI: push.yml (same deterministic entry points + coverage reachability +
  diff-scoped mutation), on push and by hand. Changed source modules block on
  new survivors; baseline/config changes verify every fingerprint. weekly.yml
  runs full mutmut, pip-audit and issue handoff on the first day of each month
  at 04:00 UTC and when manually dispatched. ci.yml keeps only the product's
  dependency audit and Docker build; live.yml is the nightly tmapi smoke.

Every Pawl mutation entry point forces an installed pytest-xdist plugin to
main-process mode (`-p xdist.plugin -n0`) and rejects explicit xdist controls
in mutmut's later pytest arguments. Active xdist distribution is unsafe for
mutmut's statistics and exit-state classification.

Mutation testing is enforced on push, not commit. `MUTANTS.md` is the accepted
survivor baseline; a header-only file accepts none. `[tool.mutmut]
source_paths` is `["app"]`; `mech` stays out of it.

Running mutation locally on macOS: set `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`.
Mutants that make httpx build its default transport read the system proxy
settings inside mutmut's forked worker, which aborts under ObjC fork safety.
mutmut reports that as `suspicious`, and Pawl fails closed on it. Linux (push
CI) is unaffected.

The Pawl suite (`mech/tests/`) is not part of `uv run pytest` (`testpaths =
["tests"]`). Run it with `uv run python -m mech.selftest --record` whenever a
compatibility input changes (`pyproject.toml`, `uv.lock`, `.python-version`,
`pyrightconfig.json`, `.codex/hooks.json`, `mech/`, `.githooks/`,
`.github/workflows/`); the commit gate rejects a stale `.pawl-selftest.json`.
That file is tracked on purpose.

## Ruff is the only linter/formatter
Do not reach for black, flake8, isort, or pylint — `select` already covers them
(E/W, F, I, UP, plus this repo's B, C4, SIM, RUF, FAST). Invoke ruff as
`uv run ruff ...` or `.venv/bin/ruff`; a bare `ruff` may resolve outside the venv
to a version the gates do not run, so it can pass here and fail at the commit
gate.

Config lives in `pyproject.toml` under `[tool.ruff]`. Never add `ruff.toml` or
`.ruff.toml`: ruff prefers a standalone file over `[tool.ruff]`, so adding one
silently re-scopes every gate that shells out to ruff while `pyproject.toml`
still reads as authoritative.

`mech/` is held to Pawl's own rule set and formatting, not this repo's wider
width and extra rules (`per-file-ignores` and `[tool.ruff.format] exclude`; see
DEVIATIONS.md). Do not "fix" `mech/` to this repo's style — it is synced from
upstream, and every local edit there is drift a future sync has to untangle.

`extend-exclude = ["*.md"]` is load-bearing. ruff >=0.16 formats Python code
blocks **inside Markdown**. This repo's README and CHANGELOG are prose with
illustrative snippets, not source, and the CHANGELOG is a record of what shipped;
reformatting it makes that record unfaithful without changing a line of source.
If you ever want doc snippets formatted, delete the exclude in its own commit so
the rewrite is reviewable.

No bare `# noqa`; always `# noqa: <CODE>` with a reason. `PGH004` enforces this
in the gate, and `PGH003` does the same for blanket `# type: ignore` — both must
be in `[tool.ruff.lint] select` or this paragraph is documentation rather than a
rule. For pyright, use `# pyright: ignore[<rule>]` with the reason in a comment.
This repo selects `RUF`, so `RUF100` (unused noqa) is active here, unlike
upstream.

## Per-clone arming
`git config core.hooksPath .githooks` — local config, never committed, so **every
clone is unarmed until a human runs it**. Validated by `mech.validation`; a clone
without it is unarmed and silently ungated.

## Gate-config changes: convention, not mechanism
Agents may edit the gate config (`.codex/hooks.json`, `.claude/settings.json`,
`mech/`, tool configs, `.githooks/`, workflows). Say what you are changing and
why *before* changing it — a gate that quietly weakens itself is worth less than
no gate. Don't use `git commit --no-verify`; if the gate is wrong, fix the gate.

## MUTANTS.md is the push mutation baseline
Five cells: `| mutant ID | class | reason | guard-or-reopen-condition | mutation
fingerprint |`. Three classes only: `equivalent`, `environment-equivalent`,
`guarded-elsewhere`. The fifth cell is 12 lowercase hex binding the row to the
mutation at its ID, because mutmut numbers mutants per function — editing a
baselined function re-points every ordinal at or after the edit and the reason
silently stops describing the mutant. `mutation.fingerprints([id], root)`
computes one.

The commit path does not read it. Push CI reads it for eligible source,
baseline, and mutation-config diffs; new survivors and relevant stale
fingerprints are red. `mech.ci_weekly` still performs the full reconciliation
on demand.

## DEVIATIONS.md — departures from stock pawl
One row per deliberate departure, written at decision time, tagged
`[port]`/`[scope]`/`[floor]`/`[pin]`/`[waiver]`, with a removal condition. The
removal condition is the point: it is what keeps a `[floor]` or `[scope]` row
from silently becoming permanent. A row with no removal condition is a decision
to live with something forever and should say so explicitly.

**Ask which kind of deviation you are writing, because it decides the removal
condition:**

- **Convergent** — you fixed something stock pawl got wrong, and stock would be
  better off with your fix. The likeliest way this row ends is *upstream
  adopting it*, so the condition must say so: **"…or when pawl ships an
  equivalent."** Without that clause the row cannot retire itself, and it
  survives as a claimed difference after the difference is gone.
- **Divergent** — a fact about this repo that upstream will never converge on:
  the interpreter pin, house line width, a suppression premise, suite runtime.
  Naming upstream here would be noise; the condition is about the local fact
  changing.

The `stock behaviour replaced` cell is what a reader consults to answer *"what
happens if I revert this?"* — keep it true, or it misdirects the person being
most careful.

Open the file with `adopted-from: <pawl sha>` and `last-synced: <pawl sha>`.
Those two lines turn the next sync from a file diff into `git log <sha>..HEAD`.
An unrecorded difference is treated as rot by a future `pawl-sync`.

`| date | surface | deviation | why | stock behaviour replaced | removal condition |`

## FIXES.md / ESCAPES.md — defect telemetry
FIXES.md for a defect tied to a production incident, a violation of a documented
security/safety/data-integrity/domain invariant, or a recurrence of a prior fix.
ESCAPES.md for anything below that bar found *after* the commit gate certified
the containing code. Six fields: `date | issue or fix ID | defect | cause or
mechanism | correction or decision | verification`, with the cause prefixed
exactly one of `[shape]`/`[logic]`/`[env]`/`[spec]`. Create either file on its
first entry.

## Typing ratchet (scoped)
pyright `standard` remains the project-wide floor. Strict is active only for
`mech/baseline.py`, `mech/imports_analyzer.py`, `mech/source_inventory.py`, and
`mech/stoprun.py`; those
Pawl-owned modules are measured strict-clean and travel as a unit. Do not widen
strict mechanically. Evaluate new production files at birth and enroll only
where strict is clean and the file has a meaningful typed contract; tests stay
standard. Existing files promote after deliberate cleanup, never through an
error baseline.
