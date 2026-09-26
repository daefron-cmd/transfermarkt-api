"""Commit gate orchestration (spec §6.2). Fail-closed, short-circuiting,
600 s watchdog."""

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable

from mech import imports_analyzer, paths, source_inventory, stoprun, tools, verdict

BUDGET_S = 600
SCOPE_RE = re.compile(
    r'^pyright_scope\s*=\s*"(whole|staged_plus_dependents)"', re.MULTILINE
)
Step = tuple[str, Callable[[], "str | None"]]


class GateError(Exception):
    """Infrastructure-class gate failure (REQ-ARCH-6: `error`, never `fail`):
    a required tool could not run, or pytest collected zero tests. Raised
    from a step's `_run` closure so `run_steps` short-circuits immediately;
    `run_gate` catches it and maps to a `verdict.errored` outcome naming the
    step, distinct from a tool-reported violation (a plain string return,
    which becomes `fail`)."""

    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step
        self.message = message


PUSH_CI_RE = re.compile(r"-m\s+mech\.ci_push\b")


def push_ci_present(root: str) -> bool:
    """Does a committed workflow reference `mech.ci_push`, outside a comment?

    `staging_warning` below tells the user a workflow references push CI. A
    deployment may legitimately decline CI — `mech.validation` treats an
    absent `.github/workflows/` as "nothing to check yet", not an error — and
    on such a repo that sentence is false in the one place the user is being
    told not to worry.

    Best-effort by design (BACKLOG 15): a text match over comment-stripped
    lines, not a YAML parse — it cannot see whether the invocation sits in a
    job reachable from a `push` trigger, which is why `staging_warning` says
    "references" rather than "runs". Stripping treats everything from the
    first `#` on a line as comment; YAML is looser (a `#` inside a quoted
    scalar is data), so an invocation on such a line is missed — the error
    direction that reads as a missing backstop, never a promised one.

    Deliberately a local matcher rather than a reach into `validation`'s private
    workflow discovery: this asks a narrower question (is *push* CI here), and
    the module boundary is worth more than the five shared lines. The drift that
    buys is pinned by a test asserting this returns True for pawl itself.
    """
    repository_root = paths.repo_root(root) or root
    workflows = os.path.join(repository_root, ".github", "workflows")
    if not os.path.isdir(workflows):
        return False
    for name in sorted(os.listdir(workflows)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflows, name), encoding="utf-8") as fh:
            code = "\n".join(line.split("#", 1)[0] for line in fh.read().splitlines())
            if PUSH_CI_RE.search(code):
                return True
    return False


def staging_warning(status_porcelain: str, *, has_push_ci: bool = True) -> "str | None":
    """Report where the working tree exceeds what is being committed, or None.

    REQ-CG-2 used to make this a *precondition*: any unstaged change or
    untracked file aborted the gate, so that "the tree is green" and "this
    commit is green" were the same statement. Every step below still runs
    against the working tree, so that identity is what made the gate's
    certification claim true.

    Downgraded to a warning on 2026-07-25. Blocking forced every partial
    commit into an all-or-nothing one — three times it merged two logical
    changes into a single commit, including the one that removed it. The
    guarantee is not abandoned, it narrows: `push.yml` runs the same
    deterministic checks against the PUSHED TIP (checkout of `github.sha`), so a staged
    subset is caught at push if it is still the tip by then — the
    intermediate commits of a multi-commit push are never individually
    verified (panel 2026-08-02, item 7; accepted as a solo-repo trade,
    BACKLOG 17). A slower, narrower loop on a failure mode that is rare and
    locally recoverable.

    `has_push_ci` exists because that relocation is not available to every
    deployment. Where CI was declined the guarantee does not move, it is simply
    gone, and the message says so rather than promising a run that will never
    happen. Callers pass `push_ci_present(root)`; the default is True so that
    the pure-string tests above stay pure.

    Both categories are reported because both mean the same thing — the tree
    is a superset of the commit. Untracked files are the "forgot to git add
    the new module" case; unstaged changes are the "staged half the work" one.
    """
    unstaged, untracked = [], []
    for line in status_porcelain.splitlines():
        if line.startswith("??"):
            untracked.append(line[3:])
        elif len(line) >= 2 and line[1] != " ":
            unstaged.append(line[3:])
    if not unstaged and not untracked:
        return None
    parts = ["staged subset — the gate verified the WORKING TREE, not this commit."]
    if unstaged:
        parts.append(f"  unstaged: {', '.join(sorted(unstaged))}")
    if untracked:
        parts.append(f"  untracked: {', '.join(sorted(untracked))}")
    parts.append(
        "  a committed workflow references push CI — the same deterministic checks "
        "against the pushed tip."
        if has_push_ci
        else "  no push CI in this deployment — nothing re-checks the commit itself."
    )
    return "\n".join(parts)


def pyright_scope(pyproject_text: str) -> str:
    match = SCOPE_RE.search(pyproject_text)
    return match.group(1) if match else "whole"


def run_steps(
    steps,
    budget_s: int = BUDGET_S,
    clock=time.monotonic,
    start: "float | None" = None,
) -> "tuple[str, str] | None":
    """Run (name, fn) steps in order; fn returns error text or None. Short-circuit.

    `fn` may also raise `GateError` (infrastructure-class failure); that
    propagates uncaught here by design — the caller (`run_gate`) is the one
    that knows how to map it to a verdict, and letting it propagate stops
    the loop immediately, exactly like a truthy return value would.

    `start` is the instant the budget began; `run_gate` passes its own, so
    this aggregate check and the per-slot `remaining()` clamp measure from
    the same instant and pre-loop time (`git status`, the staging warning)
    counts against the watchdog. Defaults to now for standalone use.
    """
    start = clock() if start is None else start
    for name, fn in steps:
        if clock() - start > budget_s:
            return name, (
                f"Commit gate timeout: step '{name}' exceeded {budget_s} s total "
                "runtime"
            )
        error = fn()
        if error is not None:
            return name, error
    return None


def format_pytest_failure(stdout: str, stderr: str) -> str:
    """REQ-T2-3/B-33: the seed line survives truncation or `seed unavailable`
    is appended — never rely on the tail window keeping it."""
    match = stoprun.SEED_RE.search(stdout + stderr)
    seed = match.group(0) if match else "seed unavailable"
    return f"{stdout[-3000:]}{stderr[-1000:]}\n{seed}"


PYTEST_INFRA_EXITS = {2: "interrupted", 3: "internal error", 4: "usage error"}
TOOL_INFRA_EXITS = {
    "ruff-format": {2: "tool or configuration error"},
    "ruff-check": {2: "tool or configuration error"},
    "pyright": {
        2: "fatal error",
        3: "configuration error",
        4: "command-line error",
    },
}


def _tool_step(
    argv: list[str],
    root: str,
    name: str,
    timeout: int = 120,
    no_tests_is_error: bool = False,
    run=subprocess.run,
    remaining: "Callable[[], float] | None" = None,
):
    """Build one gate step closure around a tool invocation.

    Verdict boundary (REQ-ARCH-6): a plain string return is `fail` — the tool
    ran to completion and reported against the code. `GateError` is `error` —
    no verdict exists: the process could not be spawned, was killed by a
    signal (negative returncode), or is pytest reporting about the run rather
    than the code (exit 5 collected-no-tests under `no_tests_is_error`, and
    exits 2/3/4: interrupted, internal error, usage error), or ruff/pyright
    returned one of their documented infrastructure exits. Exit 1 remains a
    code verdict for both ruff and pyright; unknown positive exits remain
    failures unless the owning tool's contract says otherwise.
    """

    def _run() -> "str | None":
        # REQ-ARCH-9: per-slot timeout clamped to the remaining watchdog budget
        limit = timeout if remaining is None else max(1, min(timeout, int(remaining())))
        try:
            proc = run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
                env=tools.scrubbed_env(),
            )
        except Exception as exc:  # noqa: BLE001  # fail-closed (REQ-CG-5)
            msg = f"{name} could not run: {type(exc).__name__}: {exc}"
            raise GateError(name, msg) from exc
        if proc.returncode < 0:
            msg = f"{name} killed by signal {-proc.returncode} — no verdict produced"
            raise GateError(name, msg)
        if no_tests_is_error and proc.returncode == 5:  # REQ-CG-3 step 4
            msg = f"{name} collected no tests — infrastructure error (fail-closed)"
            raise GateError(name, msg)
        if name == "pytest" and proc.returncode in PYTEST_INFRA_EXITS:
            meaning = PYTEST_INFRA_EXITS[proc.returncode]
            tail = f"{proc.stdout[-3000:]}{proc.stderr[-1000:]}"
            msg = (
                f"{name} exited {proc.returncode} ({meaning}) — "
                f"no verdict produced\n{tail}"
            )
            raise GateError(name, msg)
        meaning = TOOL_INFRA_EXITS.get(name, {}).get(proc.returncode)
        if meaning is not None:
            tail = f"{proc.stdout[-3000:]}{proc.stderr[-1000:]}"
            msg = (
                f"{name} exited {proc.returncode} ({meaning}) — "
                f"no verdict produced\n{tail}"
            )
            raise GateError(name, msg)
        if proc.returncode != 0:
            if name == "pytest":
                return (
                    f"{name} failed:\n{format_pytest_failure(proc.stdout, proc.stderr)}"
                )
            tail = f"{proc.stdout[-3000:]}{proc.stderr[-1000:]}"
            return f"{name} failed:\n{tail}\nseed unavailable"
        return None

    return _run


def _staged_files(root: str, timeout: int = 120) -> list[str]:
    # Deliberately calls real `subprocess.run` (not the `run` seam threaded
    # through the rest of this module): tests monkeypatch `_staged_files`
    # itself rather than scripting a `run` callable for it.
    # `-z` because plain `--name-only` C-quotes any path with a special
    # character (`"weird\tname.py"`), which then fails `.endswith(".py")`
    # downstream; NUL-separated output is never quoted.
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--relative"],  # noqa: S607  # PATH-resolved `git` is intentional
        cwd=root,
        capture_output=True,
        text=False,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = os.fsdecode(proc.stderr[-500:]).strip()
        raise OSError(f"git staged-file discovery exited {proc.returncode}: {detail}")
    return [os.fsdecode(name) for name in proc.stdout.split(b"\0") if name]


def _dotted(rel: str) -> str:
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def _pyright_step(
    root: str,
    argv: "list[str]",
    run=subprocess.run,
    timeout: int = 120,
    remaining: "Callable[[], float] | None" = None,
):
    def _run() -> "str | None":
        with open(f"{root}/pyproject.toml", encoding="utf-8") as fh:
            scope = pyright_scope(fh.read())
        argv_local = list(argv)
        if scope == "staged_plus_dependents":
            # staged .py set = every staged .py file regardless of source_paths;
            # deleted paths feed importer discovery only, never the file list
            # (REQ-CG-3)
            limit = (
                timeout if remaining is None else max(1, min(timeout, int(remaining())))
            )
            try:
                staged = [
                    f for f in _staged_files(root, timeout=limit) if f.endswith(".py")
                ]
            except Exception as exc:  # noqa: BLE001  # fail-closed discovery
                msg = (
                    "pyright staged-file discovery could not run: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise GateError("pyright", msg) from exc
            live = [f for f in staged if os.path.isfile(os.path.join(root, f))]
            if staged:
                try:
                    inventory = source_inventory.load(root)
                except source_inventory.InventoryError as exc:
                    return f"source inventory failed: {exc}"
                seeds = {
                    m
                    for m in (inventory.module_for(f) or _dotted(f) for f in staged)
                    if m
                }
                try:
                    closure = imports_analyzer.importers_closure(root, seeds)
                except imports_analyzer.AnalyzerError as exc:
                    return f"import analyzer failed: {exc}"
                files = live + [m.replace(".", "/") + ".py" for m in closure]
                if files:  # no live staged file and no importer ⇒ whole-project
                    argv_local += sorted(set(files))
        return _tool_step(
            argv_local, root, "pyright", timeout=timeout, run=run, remaining=remaining
        )()

    return _run


def _build_steps(
    root: str, table=tools.GATE, run=subprocess.run, remaining=None
) -> "list[Step]":
    """REQ-ARCH-9: gate steps come from the tools.GATE table, not inline argv;
    `run`/`remaining` thread through every step builder."""
    built = []
    for slot, argv, timeout_s, scope_mode in table:
        if scope_mode == "pyright-scoped":
            built.append(
                (
                    slot,
                    _pyright_step(
                        root,
                        list(argv),
                        run=run,
                        timeout=timeout_s,
                        remaining=remaining,
                    ),
                )
            )
        else:  # "repo"
            built.append(
                (
                    slot,
                    _tool_step(
                        list(argv),
                        root,
                        slot,
                        timeout=timeout_s,
                        no_tests_is_error=(slot == "pytest"),
                        run=run,
                        remaining=remaining,
                    ),
                )
            )
    return built


def run_gate(root: str, run=subprocess.run, clock=time.monotonic) -> dict:
    """The `run`/`clock` seams are the public contract (tested via injection —
    Global Constraints); they thread into every step builder."""
    start = clock()
    try:
        proc = run(
            # --untracked-files=all overrides status.showUntrackedFiles=no,
            # which would hide the "forgot to add the new module" case.
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=BUDGET_S,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001  # fail-closed watchdog boundary
        return verdict.errored(
            f"staging discovery could not run: {type(exc).__name__}: {exc}",
            tool="staging",
        )
    if proc.returncode != 0:
        # Empty stdout from a FAILED git status is byte-indistinguishable from a
        # clean tree, so staying quiet here would certify silently in the one
        # case where the gate cannot know what the commit contains. Advisory
        # rather than fail-closed: the four steps below still run against the
        # working tree, and only the report of how tree and index differ is lost.
        print(
            f"staging: could not determine what this commit contains — "
            f"`git status` exited {proc.returncode}: {proc.stderr.strip()[-500:]}",
            file=sys.stderr,
        )
    warning = staging_warning(proc.stdout, has_push_ci=push_ci_present(root))
    if warning:
        # stderr, not a finding: the verdict schema (REQ-ARCH-6) has no
        # non-blocking channel, and inventing one would make every caller
        # decide whether a warning counts as a failure. The commit proceeds.
        print(f"staging: {warning}", file=sys.stderr)

    def remaining() -> float:
        return BUDGET_S - (clock() - start)

    try:
        failure = run_steps(
            _build_steps(root, run=run, remaining=remaining), clock=clock, start=start
        )
    except GateError as exc:
        return verdict.errored(exc.message, tool=exc.step)
    if failure:
        step, error = failure
        return verdict.failed(
            [verdict.finding(step, error)], summary=f"{step} blocked the commit"
        )
    return verdict.passed()
