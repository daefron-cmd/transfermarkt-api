"""Declarative tool-invocation tables (REQ-ARCH-9).

Includes the execution *context* a spawned tool gets (`SCRUB_ENV`), not just its
argv: both are part of what "invoking a tool" means here, and both callers need
the same answer.

Internal structure, not interface: consumed by gate/CI runner functions.
Never loaded from files; no profile format; no language selection.

A `TIER1` table lived here until 2026-07-25. Nothing registered
`mech.hook_tier1`, and the table had drifted from what actually runs — it
carried a pyright row and formatted before autofixing. Per-edit diagnostics
are a user-level ruff hook now, outside this repo entirely; pyright stays in
editor diagnostics and the commit gate.
"""

import os

# Required verdicts use one stable test order for a given revision. The
# report-only Stop hook intentionally omits this argument, so it continues to
# explore other orders without making commit/push certification stochastic.
REQUIRED_PYTEST_SEED_ARG = "--randomly-seed=20260901"

# Ambient state that would change the meaning of a spawned tool's run, removed
# from the environment every mech-spawned tool receives. Two families:
#
# - PYTEST_*/COVERAGE_*: ambient config that silently redefines the run. An
#   exported PYTEST_ADDOPTS="-k nothing" turns a suite into "no tests collected".
# - GIT_*: everything that aims a git command somewhere other than where a plain
#   shell would aim it — at another repo/index, or at config the developer never
#   set. The gate spawns tools only from inside a git hook, which is the one
#   moment git exports these: `git commit -- <paths>` points GIT_INDEX_FILE at
#   the index it is about to commit, and pytest inherited it, so tests building
#   their own repository read and WROTE that index (2026-07-27).
#   GIT_CONFIG_PARAMETERS is the config-injection case — `git -c k=v commit`
#   exports it, as JetBrains does on essentially every command.
#
# - UV_*: every row in GATE and CI below begins with `uv run`, so a variable
#   that redirects uv redirects the entire gate — a strictly wider hole than
#   the GIT_* one above, and found the same way (cross-lineage panel,
#   2026-08-02). Measured: with `UV_WORKING_DIR` exported, the gate returns
#   exit 0 and zero findings on a tree carrying four real ruff violations,
#   because `uv` chdirs before running and each step checked the other
#   directory — `cwd=root` on the subprocess does not prevent it.
#   `UV_PROJECT` redirects project discovery the same way. `UV_ENV_FILE` is
#   the second-order case: it loads a .env into the child, which re-injects
#   the PYTEST_* names this very function just removed. `UV_PYTHON` silently
#   changes which interpreter the checks run under.
#
# Deliberately kept: GIT_AUTHOR_*/GIT_EDITOR (in-flight commit context, but
# misattributing a test commit is a lesser class of harm, and scrubbing could
# not restore the user's shell values anyway) and GIT_EXEC_PATH (locates git's
# own helpers; redirects nothing). Also kept: UV_FROZEN/UV_LOCKED/UV_NO_SYNC
# and the UV_INDEX/UV_CACHE_DIR family — they change how the environment is
# resolved, not which tree is checked, and scrubbing them would break
# legitimate offline and pinned-index setups.
#
SCRUB_ENV: tuple[str, ...] = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "COVERAGE_PROCESS_START",
    "GIT_INDEX_FILE",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH",
    "GIT_PREFIX",
    "UV_WORKING_DIR",
    "UV_PROJECT",
    "UV_ENV_FILE",
    "UV_PYTHON",
)


def scrubbed_env() -> dict[str, str]:
    """REQ-ARCH-9 execution context: the environment a spawned tool receives."""
    return {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}


# REQ-ARCH-9 / REQ-CG-4: mutmut adapter argv, consumed by mech.mutation.run_scoped.
# "{module}" is substituted with the scoped module (glob-suffixed) before exec.
MUTMUT_RUN: tuple[str, ...] = ("uv", "run", "mutmut", "run", "{module}")
MUTMUT_RESULTS: tuple[str, ...] = ("uv", "run", "mutmut", "results")
# REQ-ARCH-9: timeout for the standalone `mutmut results` call ci_weekly.main
# makes after its full run (mutation.run_scoped's own MUTMUT_RESULTS calls
# reuse that function's own `timeout` param instead — see mutation.py).
MUTMUT_RESULTS_TIMEOUT: int = 60

# REQ-CI-3a (E1): the weekly retire classification must see killed mutants,
# and plain `mutmut results` omits them (fixture-pinned) — a baselined mutant
# that is now killed would misfile as retire-as-obsolete instead of
# retire-as-satisfied. Weekly-only argv; push/diff-scoped flows keep
# MUTMUT_RESULTS. mutmut 3.7 wire form is `--all BOOLEAN`.
MUTMUT_RESULTS_ALL: tuple[str, ...] = (
    "uv",
    "run",
    "mutmut",
    "results",
    "--all",
    "true",
)

# Commit-gate steps, in run order. scope_mode: "repo" = fixed argv;
# "pyright-scoped" = argv may be narrowed per [tool.mech] pyright_scope.
# Every row's timeout_s is consumed by `gate._build_steps`, which threads it
# (alongside the watchdog `remaining` callable) into whichever step factory
# owns that scope_mode — including the pyright slot, not just "repo" — so the
# per-slot budget and the REQ-ARCH-9 watchdog clamp both apply uniformly.
#
# Mutation testing is deliberately NOT a commit-gate step. Diff-scoped mutation
# returned to push CI on 2026-09-01, after Pawl tests moved behind the `mech/`
# ownership boundary; the local gate remains the fast deterministic loop.
#
# `--no-cache` on ruff-check is load-bearing, not tidiness (2026-08-02). Ruff's
# per-file cache key is (mtime, permission mode) — not content, and not the
# state of any *other* file. But isort's verdict depends on exactly that other
# state: `pkg.mod` is first-party or third-party according to whether `pkg/mod`
# exists on disk. Create that module and every untouched importer's cached
# I001 verdict silently stops being true. Tier 1 makes this routine rather than
# exotic: it runs `ruff check` on the file it just wrote, mid-edit, which under
# test-first development is always *before* the module under test exists — so
# it caches a clean verdict that the very next file creation falsifies.
# Measured downstream 2026-08-02: the gate certified 19 commits over two files
# a cold run rejected. Cost of closing it, measured in THIS repo (27 Python
# files): ~30 ms either way — three paired runs put cached at 35/27/29 ms and
# uncached at 31/32/30 ms, i.e. cached was slower twice. There is nothing to
# trade. The 0.018 s → 0.020 s figure quoted elsewhere on 2026-08-02 was
# measured on the cross-review deployment (103 files), not here; do not carry a
# downstream number back upstream as if it described pawl.
# `ruff-format` needs no such flag — formatting reads one file and nothing else.
GateRow = tuple[str, tuple[str, ...], int, str]
GATE: tuple[GateRow, ...] = (
    ("ruff-format", ("uv", "run", "ruff", "format", "--check", "."), 120, "repo"),
    ("ruff-check", ("uv", "run", "ruff", "check", "--no-cache", "."), 120, "repo"),
    ("pyright", ("uv", "run", "pyright"), 120, "pyright-scoped"),
    (
        "pawl-compatibility",
        ("uv", "run", "python", "-m", "mech.selftest", "--check"),
        60,
        "repo",
    ),
    (
        "pytest",
        ("uv", "run", "pytest", REQUIRED_PYTEST_SEED_ARG),
        300,
        "repo",
    ),
)

# REQ-CI-0 / REQ-ARCH-9: every tool subprocess invocation made by ci_push /
# ci_weekly (mech/ci_push.py, mech/ci_weekly.py). "{...}" placeholders are
# substituted per call, same convention as TIER1/GATE. mutmut argv for the
# push (diff-scoped) run reuses MUTMUT_RUN/MUTMUT_RESULTS above via
# mutation.run_scoped; only the weekly full run needs its own row here
# (unscoped, no per-module filter) — MUTMUT_RESULTS is reused for it too.
CIRow = tuple[str, tuple[str, ...], int]
CI: tuple[CIRow, ...] = (
    ("ci-validation", ("uv", "run", "python", "-m", "mech.validation"), 60),
    ("ci-ruff-format", ("uv", "run", "ruff", "format", "--check", "."), 120),
    # `--no-cache` for the same reason as the GATE row above. A CI runner is
    # cold today, so this changes nothing now — it is here so that adding a
    # `.ruff_cache` restore step later cannot silently re-open the hole in the
    # backstop that catches what the local gate misses.
    ("ci-ruff-check", ("uv", "run", "ruff", "check", "--no-cache", "."), 120),
    ("ci-pyright", ("uv", "run", "pyright"), 120),
    (
        "ci-coverage-run",
        (
            "uv",
            "run",
            "coverage",
            "run",
            "-m",
            "pytest",
            REQUIRED_PYTEST_SEED_ARG,
        ),
        300,
    ),
    (
        "ci-coverage-json",
        ("uv", "run", "coverage", "json", "-o", ".mech/coverage.json"),
        60,
    ),
    (
        "ci-git-merge-base",
        ("git", "merge-base", "HEAD", "origin/{default_branch}"),
        30,
    ),
    (
        "ci-git-diff",
        (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--relative",
            "--diff-filter=AMR",
            "{range}",
        ),
        30,
    ),
    ("ci-mutmut-run-full", ("uv", "run", "mutmut", "run"), 5400),
    ("ci-uv-export", ("uv", "export", "--frozen", "--no-hashes"), 60),
    (
        "ci-pip-audit",
        ("uv", "run", "pip-audit", "-r", ".mech/requirements.txt", "-f", "json"),
        300,
    ),
    (
        "ci-gh-issue-list",
        (
            # "isPullRequest" is not a valid `gh issue list --json` field
            # (validated live against gh 2.94.0 on 2026-07-23: "Unknown JSON
            # field") and `gh issue list` never returns PRs in the first
            # place, so it isn't needed — see canonical_issues in
            # ci_weekly.py.
            "gh",
            "issue",
            "list",
            "--label",
            "mech-weekly",
            "--state",
            "open",
            "--json",
            "number,title,body,comments,labels",
        ),
        30,
    ),
    ("ci-gh-label-create", ("gh", "label", "create", "mech-weekly", "--force"), 15),
    (
        "ci-gh-issue-create",
        (
            "gh",
            "issue",
            "create",
            "--title",
            "{title}",
            "--label",
            "mech-weekly",
            "--body",
            "{body}",
        ),
        30,
    ),
    (
        "ci-gh-issue-comment",
        ("gh", "issue", "comment", "{number}", "--body", "{body}"),
        30,
    ),
)
