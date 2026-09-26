"""mutmut adapter: path->module map, results parsing, state taxonomy (REQ-CG-4).

Wire contract: mutant IDs and result states are read verbatim from the
pinned mutmut's `results` output. Result-state taxonomy per REQ-CG-4: the
detected states (`killed`; `timeout` — a hung suite is a detection, and
the timeout set is machine-load-dependent) pass; `survived` enters
new-survivor comparison; every other state (including an unparseable
record) is an error — fail-closed abort naming the state. PASSING_STATES
and SURVIVOR_STATES are pinned to the vocabulary observed in a real
capture (`mech/tests/fixtures/mutmut_results.txt`, mutmut 3.6.0 against this
repo, replayed against 3.7.0): default `mutmut results` output only ever
contains `survived` and `no tests` (killed mutants are omitted by default);
the weekly flow uses
MUTMUT_RESULTS_ALL (`--all true`), whose listing adds `killed` and
`not checked` (pinned in mech/tests/fixtures/mutmut_results_all.txt).
`no tests` (mutmut's rendering of "untested" — no test exercises the mutant)
is deliberately left out of both sets, so it fails closed per the
spec's named `untested` state class.

Concurrent use is unsupported, by decision (2026-08-02, cross-lineage
worklist item 7): `mutmut run` and `mutmut results` share mutable state
under `mutants/` with no locking of mutmut's own, so two concurrent
invocations on one root can interleave into a clean verdict over mutants
this process never examined. Nothing in the stack runs the adapter
concurrently — the gate, the weekly and the operator path are all serial —
and a lockfile here would not cover a bare `mutmut` the operator runs by
hand, so it would guard the one path that already cannot race. Reopen if
any caller ever parallelizes mutation runs on a shared root.
"""

import ast
import difflib
import hashlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import textwrap
import tokenize
import tomllib
from collections.abc import Callable

from mech import tools

PASSING_STATES = {"killed", "timeout"}
SURVIVOR_STATES = {"survived"}

# `skipped` has no captured fixture but is in the vocabulary deliberately:
# mutmut emits it for a deliberate `# pragma: no mutate`. Treating an
# intentional marker as crash noise would retry every module containing one
# and then fail closed. Capture a `skipped` fixture opportunistically.
KNOWN_STATES = (
    PASSING_STATES
    | SURVIVOR_STATES
    | {
        "no tests",
        "not checked",
        "skipped",
    }
)


def _transient_states(states: dict[str, list[str]]) -> list[str]:
    """Crash-like state names in `states`, sorted and deduplicated.

    Defined by exclusion because no `segfault` fixture exists to pin the
    literal token. `UNPARSEABLE_STATE` is excluded on purpose: it is the
    adapter's own signal, and retrying it could mask a garbled `results`
    blob as a clean pass on the second attempt.
    """
    return sorted(s for s in states if s not in KNOWN_STATES and s != UNPARSEABLE_STATE)


# Captured verbatim (task-12-report.md dogfood run) from a real mutmut 3.6.0
# crash: `uv run mutmut run "mech.tools*"` raises this AssertionError (exit 1)
# when the module is pure declarative data (module-level tuples only, no
# function/class bodies) and mutmut's own `--mutant` filter matches none of
# its zero-mutant catalog. Not a real tool failure — REQ-CG-4 requires this
# specific, anticipated case to pass the module through with an INFO line,
# never fail-closed like a genuine mutmut crash. Match text, not exit code:
# the crash and a real failure both exit 1.
ZERO_MUTANTS_SIGNATURE = "Filtered for specific mutants, but nothing matches"
COLLECT_ONLY_FILTER = "mech.__collect_only_no_such_mutant__*"
# Synthetic state used by parse_results for non-empty, unparseable input —
# never a real mutmut state string — so classify's existing fail-closed
# branch (any state outside PASSING_STATES/SURVIVOR_STATES) fires on it
# instead of an unparseable blob silently reading as a zero-mutant pass.
UNPARSEABLE_STATE = "unparseable_mutmut_output"

# One "<id>: <state>" record per line (mutmut 3.6/3.7 shape, see fixture);
# state may contain spaces (e.g. "no tests").
RESULT_LINE_RE = re.compile(r"^\s*(\S+):\s*([a-zA-Z][a-zA-Z_ ]*)\s*$", re.MULTILINE)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def _xdist_installed() -> bool:
    return importlib.util.find_spec("xdist") is not None


def execution_env() -> dict[str, str]:
    """Return the isolated environment for every Pawl-owned mutmut run.

    mutmut forks one child per mutant. If each child starts an xdist worker
    pool, mutmut's stats process cannot reliably associate worker-run tests
    with mutants, and xdist's fail-fast exits can be recorded as interrupted
    mutants. Load the installed plugin explicitly so ``-n0`` remains valid
    when entry-point autoload is disabled; without xdist, use the ordinary
    authoritative scrubbed environment.
    """
    env = tools.scrubbed_env()
    if _xdist_installed():
        env["PYTEST_ADDOPTS"] = "-p xdist.plugin -n0"
    return env


def mutmut_pytest_args(root: str) -> "tuple[list[str], str | None]":
    """Read explicit mutmut pytest args, which apply after env addopts."""
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], f"mutation config unreadable: {type(exc).__name__}: {exc}"
    tool = data.get("tool")
    config = tool.get("mutmut") if isinstance(tool, dict) else None
    if not isinstance(config, dict):
        return [], "mutation config unreadable: [tool.mutmut] is missing"

    args: list[str] = []
    for key in ("pytest_add_cli_args_test_selection", "pytest_add_cli_args"):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(arg, str) for arg in value
        ):
            return [], f"mutation config unreadable: {key} must contain strings"
        args.extend(value)
    return args, None


def xdist_config_error(root: str) -> "str | None":
    """Reject explicit xdist controls that can override Pawl's serial mode."""
    args, error = mutmut_pytest_args(root)
    if error is not None:
        return error
    for arg in args:
        value = arg.strip().lower()
        short_n = value[2:].removeprefix("=") if value.startswith("-n") else ""
        if (
            "xdist" in value
            or value in {"-d", "-n", "--numprocesses", "--dist", "--tx"}
            or short_n.isdigit()
            or short_n in {"auto", "logical"}
            or value.startswith(("--numprocesses=", "--dist=", "--tx="))
        ):
            return (
                "mutation config may not control xdist in "
                f"pytest_add_cli_args*: {arg!r}; Pawl owns serial execution"
            )
    return None


def _mutation_config_error(root: str) -> "str | None":
    """Validate mutation config when this root actually has one."""
    if not os.path.isfile(os.path.join(root, "pyproject.toml")):
        return None
    return xdist_config_error(root)


def parse_results(text: str) -> dict[str, list[str]]:
    """Map mutmut result-line state -> mutant IDs.

    EVERY non-blank line that is not a "<id>: <state>" record lands in
    UNPARSEABLE_STATE (truncated to 200 chars), not just the all-garbage
    case: real `mutmut results` output is records-only (see the pinned
    fixtures), so an unreadable line means a garbled, truncated or foreign
    stream — and one good line must not buy silence for the rest, or a
    partly-written stream reads as a complete, clean verdict. A genuinely
    empty (or whitespace-only) `text` returns `{}`, matching the
    zero-mutants-pass contract.
    """
    states: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = RESULT_LINE_RE.match(line)
        if match:
            states.setdefault(match.group(2).strip().lower(), []).append(match.group(1))
        else:
            unreadable.append(line.strip()[:200])
    if unreadable:
        states[UNPARSEABLE_STATE] = unreadable
    return states


def classify(states: dict[str, list[str]]) -> dict:
    survivors: list[str] = []
    for state, ids in states.items():
        if not ids:
            continue
        if state in PASSING_STATES:
            continue
        if state in SURVIVOR_STATES:
            survivors.extend(ids)
        else:
            return {
                "survivors": [],
                "error": f"mutmut reported state {state!r} "
                f"({len(ids)} mutants) — fail-closed per spec REQ-CG-4",
            }
    return {"survivors": survivors, "error": None}


def _is_function_scope(scope: str, root: str) -> bool:
    """Distinguish mutmut's encoded function names from real `x_` modules.

    Mutmut prefixes encoded functions with `x_` (and class methods with `xǁ`),
    but both are also legal module-name prefixes.  An existing module wins the
    ambiguity: widening an operator's diagnostic run is safe, while narrowing a
    push-gate module to a nonexistent function scope can discard every survivor.
    """
    leaf = scope.rsplit(".", 1)[-1]
    if not leaf.startswith(("x_", "xǁ")):
        return False
    module_path = os.path.join(root, *scope.split("."))
    return not (
        os.path.isfile(f"{module_path}.py")
        or os.path.isfile(os.path.join(module_path, "__init__.py"))
    )


def _mutmut_filter(scope: str, root: str) -> str:
    """Build an exact function/class filter without matching submodules."""
    return f"{scope}*" if _is_function_scope(scope, root) else f"{scope}.x[_ǁ]*"


def _scope(states: dict[str, list[str]], scope: str, root: str) -> dict[str, list[str]]:
    """Restrict a parsed state map to one module or function's mutants.

    `UNPARSEABLE_STATE`'s pseudo-ID is an arbitrary garbage line matching no
    module prefix, so filtering it would silently drop it to an empty,
    classify-skipped catalog — a garbled `results` blob would then read as a
    clean zero-mutant pass instead of the fail-closed error REQ-CG-4/CG-5
    require. It is exempt so it always reaches the caller unfiltered.
    """
    scoped: dict[str, list[str]] = {}
    function_scope = _is_function_scope(scope, root)
    for state, ids in states.items():
        kept = (
            ids
            if state == UNPARSEABLE_STATE
            else [
                i
                for i in ids
                if (
                    i.startswith(f"{scope}__mutmut_")
                    if function_scope
                    else i.startswith((f"{scope}.x_", f"{scope}.xǁ"))
                )
            ]
        )
        if kept:
            scoped[state] = kept
    return scoped


DIAGNOSTIC_TAIL_LINES = 5
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _invocation_error(prefix: str, proc: "subprocess.CompletedProcess") -> str:
    """`prefix`, plus the last few lines mutmut printed before it failed.

    An exit code alone is not a diagnosis. mutmut runs pytest from inside
    `mutants/`, so its failures are routinely about that tree rather than about
    the repo — a test file deleted from `tests/` stays in `mutants/tests/`
    forever, because mutmut copies with `dirs_exist_ok=True` and copytree never
    deletes. That failure reads as `exit 1` on a repo whose own suite is green,
    and the cause is in the output the adapter captured and used to discard.

    Bounded, because mutmut prints one line per mutated file. ANSI-stripped,
    because pytest colours the summary lines that are worth keeping.
    """
    text = _ANSI_RE.sub("", f"{proc.stdout or ''}\n{proc.stderr or ''}")
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(kept[-DIAGNOSTIC_TAIL_LINES:])
    return f"{prefix}\n{tail}" if tail else prefix


def _adapter_error(exc: BaseException) -> str:
    """The one wording for an unexpected adapter failure.

    Both entry points can reach this from different phases — the loop, and
    `run_scoped`'s `classify` guard — so the string lives in one place; the
    guard that used to cover the second copy no longer does.
    """
    return f"mutmut adapter error: {type(exc).__name__}: {exc}"


def _run_module_once(
    module: str,
    root: str,
    run: Run,
    timeout: int,
    env: "dict | None",
    results_argv: list[str],
) -> "tuple[dict[str, list[str]], str | None]":
    """One `mutmut run` + `results` pair for one module, scoped.

    Returns `({}, None)` for a zero-mutant module. On any failure the module
    contributes nothing, so the states half is always `{}` beside an error,
    and every failure condition names the module.
    """
    argv = [
        arg.replace("{module}", _mutmut_filter(module, root))
        for arg in tools.MUTMUT_RUN
    ]
    try:
        proc = run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {}, f"mutmut run timed out for {module} after {timeout}s"
    if proc.returncode != 0:
        # REQ-CG-4 zero-mutants pass-through: a pure-data module (see
        # ZERO_MUTANTS_SIGNATURE) makes mutmut crash rather than report an
        # empty catalog. Any other nonzero exit is a real failure.
        if ZERO_MUTANTS_SIGNATURE in (proc.stdout + proc.stderr):
            print(f"INFO: no mutants generated for {module}", file=sys.stderr)
            return {}, None
        return {}, _invocation_error(
            f"mutmut run failed for {module}: exit {proc.returncode}", proc
        )

    try:
        results = run(
            results_argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {}, f"mutmut results timed out for {module} after {timeout}s"
    if results.returncode != 0:
        return {}, _invocation_error(
            f"mutmut results failed for {module}: exit {results.returncode}", results
        )

    return _scope(parse_results(results.stdout), module, root), None


def _run_module_with_retry(
    module: str,
    root: str,
    run: Run,
    timeout: int,
    env: "dict | None",
    results_argv: list[str],
) -> "tuple[dict[str, list[str]], str | None]":
    """`_run_module_once` plus one bounded retry for crash-like noise.

    Exactly one retry, never a loop: a repeat is usually the OOM killer
    (SIGKILL renders as a crash state too) and will reproduce. Never silent —
    the appendix records 93 bogus states in one incident and ~6 recurrences in
    a single adoption, and a quiet retry would hide a crash-prone suite.
    """
    states, error = _run_module_once(module, root, run, timeout, env, results_argv)
    if error is not None:
        return states, error

    transient = _transient_states(states)
    if not transient:
        return states, None

    print(
        f"INFO: retrying {module} after transient state(s): {', '.join(transient)}",
        file=sys.stderr,
    )
    # Replace, never merge: the pre-retry map is discarded here whether the
    # retry succeeds or fails, so no mutant can hold two states from one run.
    states, error = _run_module_once(module, root, run, timeout, env, results_argv)
    if error is None and _transient_states(states):
        print(
            f"INFO: {module} still reports a transient state after one retry; "
            "a repeating crash is more likely memory (SIGKILL from the OOM "
            "killer) than noise",
            file=sys.stderr,
        )
    return states, error


def _collect_modules(
    modules: list[str],
    root: str,
    run: Run,
    timeout: int,
    env: "dict | None",
    results_argv: list[str],
) -> "tuple[dict[str, list[str]], str | None]":
    """Scoped states for each module in order, stopping at the first failure.

    Returns (accumulated states, error). The states are always those of the
    modules that completed — the module that failed contributes nothing. What
    a failure *means* is the caller's decision, and that decision is the only
    thing separating this adapter's two entry points: `run_scoped` discards
    them, because a gate must not act on partial data; `survey` keeps them,
    because a diagnostic tool must not throw away work the operator paid for.
    """
    all_states: dict[str, list[str]] = {}
    try:
        for module in modules:
            states, error = _run_module_with_retry(
                module, root, run, timeout, env, results_argv
            )
            if error is not None:
                return all_states, error
            for state, ids in states.items():
                all_states.setdefault(state, []).extend(ids)
        return all_states, None
    except Exception as exc:  # noqa: BLE001  # caller decides what a failure means
        return all_states, _adapter_error(exc)


def run_scoped(
    modules: list[str],
    root: str,
    run: Run = subprocess.run,
    timeout: int = 600,  # REQ-ARCH-9 normative mutation-slot timeout
    env: "dict | None" = None,  # None ⇒ Pawl's isolated mutation context
) -> "tuple[list[str], str | None]":
    """Run mutmut restricted to `modules`, returning (survivor IDs, error).

    Fail-closed by contract: any failure returns `([], error)`, discarding
    states already collected. On a pre-loosening deployment this is a gate
    step, so that must not become `survey`'s partial-state return.

    REQ-ARCH-9: base argv comes from `tools.MUTMUT_RUN` / `tools.MUTMUT_RESULTS`
    — never inlined. `mutmut results` is fetched per module immediately after
    that module's own `mutmut run`: verified empirically (real mutmut 3.7.0
    against this repo) that `mutmut run <filter>` re-collects mutants across
    the *entire* source tree on every invocation, resetting other modules'
    recorded exit codes to `not checked`, so reading `results` once at the end
    would lose every module's data except the last.
    """
    try:
        config_error = _mutation_config_error(root)
        if config_error is not None:
            return [], config_error
        resolved_env = execution_env() if env is None else env
        all_states, error = _collect_modules(
            modules, root, run, timeout, resolved_env, list(tools.MUTMUT_RESULTS)
        )
        if error is not None:
            return [], error
        outcome = classify(all_states)
        return outcome["survivors"], outcome["error"]
    except Exception as exc:  # noqa: BLE001  # surfaces as gate error (REQ-CG-5)
        return [], _adapter_error(exc)


def survey(
    modules: list[str],
    root: str,
    run: Run = subprocess.run,
    timeout: int = 600,
    env: "dict | None" = None,
    include_killed: bool = False,
) -> "tuple[dict[str, list[str]], str | None]":
    """Raw mutmut states for `modules`, unjudged. The operator's entry point.

    Returns (state -> mutant IDs, error). It does NOT call `classify`: the
    gate needs a fail-closed verdict, an operator needs the data. A module
    with one `no tests` mutant would blind `classify` to every survivor
    beside it, which is exactly the case a burndown starts from.

    On a failure at module N the states from modules 1..N-1 are returned
    *with* the error — the failing module contributes nothing. That is the
    deliberate difference from `run_scoped`, which discards them: a gate must
    not act on partial data, a diagnostic tool must not throw away work the
    operator paid for.

    `include_killed` selects `tools.MUTMUT_RESULTS_ALL`, because plain
    `mutmut results` omits killed mutants entirely and absence is not
    confirmation of a kill.
    """
    config_error = _mutation_config_error(root)
    if config_error is not None:
        return {}, config_error
    results_argv = list(
        tools.MUTMUT_RESULTS_ALL if include_killed else tools.MUTMUT_RESULTS
    )
    resolved_env = execution_env() if env is None else env
    return _collect_modules(modules, root, run, timeout, resolved_env, results_argv)


def regenerate_tree(
    root: str,
    run: Run = subprocess.run,
    timeout: int = 600,
    env: "dict | None" = None,
) -> "str | None":
    """Rebuild the generated `mutants/` tree without mutation-testing anything.

    REQ-B-4: a baseline-only commit still has to verify every row, but it has no
    module to mutate and the local tree may be absent (gitignored, fresh clone)
    or stale. The pinned mutmut has no collect-only command, so the no-match
    abort IS the mechanism: matched on ZERO_MUTANTS_SIGNATURE text, never on exit
    code, exactly as run_scoped already does.

    Measured 9.7 s on this repo, and that figure is not collection alone —
    verified empirically that `mutmut run` executes the whole clean test suite
    before it reaches the mutant filter. Two consequences: the cost includes one
    suite run, and on a red suite this returns a regeneration failure rather
    than None (the tree is still written, but the signature never appears). That
    is harmless where it is called — the gate's pytest slot runs to green before
    the mutation slot — and stays fail-closed everywhere else. Both this path
    and run_scoped use mutmut's own pytest invocation, which disables
    pytest-randomly explicitly; varying-order exploration belongs to the
    report-only Stop hook instead.
    """
    config_error = _mutation_config_error(root)
    if config_error is not None:
        return config_error
    resolved_env = execution_env() if env is None else env
    argv = [arg.replace("{module}", COLLECT_ONLY_FILTER) for arg in tools.MUTMUT_RUN]
    try:
        proc = run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=resolved_env,
        )
    except Exception as exc:  # noqa: BLE001  # surfaces as gate error (REQ-CG-5)
        return f"mutants tree regeneration failed: {type(exc).__name__}: {exc}"
    if proc.returncode == 0 or ZERO_MUTANTS_SIGNATURE in (proc.stdout + proc.stderr):
        return None
    return _invocation_error(
        f"mutants tree regeneration failed: exit {proc.returncode}", proc
    )


FINGERPRINT_RE = re.compile(r"^[0-9a-f]{12}$")
# +/-2 normalized lines of context around each changed hunk. Zero context is
# not an identity: 196 of the 1834 mutants of the currently-baselined functions
# share a zero-context delta with a sibling (largest family: eight identical
# `text=True` -> `text=False` deltas on eight subprocess calls in
# ci_weekly.main). At +/-2 no two mutants of one function collide.
FINGERPRINT_CONTEXT = 2
_MUTANT_ID_RE = re.compile(
    r"^(?P<module>\S+)\.(?P<mangled>\S+)__mutmut_(?P<ordinal>\d+)$"
)


def _generated_path(module: str, root: str) -> "str | None":
    """Generated-tree file for a dotted module, or None if absent."""
    base = os.path.join(root, "mutants", *module.split("."))
    for candidate in (f"{base}.py", os.path.join(base, "__init__.py")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _function_bodies(
    module: str, root: str
) -> "tuple[dict[str, list[str]] | None, str | None]":
    path = _generated_path(module, root)
    if path is None:
        return None, f"no generated module for {module!r}"
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        return None, f"generated module unreadable: {type(exc).__name__}"
    lines = source.splitlines()
    return {
        node.name: lines[node.lineno - 1 : node.end_lineno]
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }, None


def _normalized_lines(src_lines: "list[str]") -> "list[str]":
    """Comment-free, whitespace-collapsed, blank-free rendering.

    Comments are dropped because mutmut never mutates them, so a comment edit
    on a mutated line (commit 6f7d577 was exactly that) must not re-open a row.
    The whole function is tokenized at once — a single continuation line is not
    tokenizable on its own; an untokenizable body degrades to comment-bearing
    text rather than failing.
    """
    lines = textwrap.dedent("\n".join(src_lines)).splitlines()
    try:
        tokens = list(
            tokenize.generate_tokens(io.StringIO("\n".join(lines) + "\n").readline)
        )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    else:
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
    return [c for c in (re.sub(r"\s+", " ", ln).strip() for ln in lines) if c]


def _emission(
    bodies: "dict[str, list[str]]", mangled: str, ordinal: str
) -> "tuple[list[str] | None, str | None]":
    orig_name, mutant_name = f"{mangled}__mutmut_orig", f"{mangled}__mutmut_{ordinal}"
    if orig_name not in bodies:
        return None, f"{orig_name} absent from the generated tree"
    if mutant_name not in bodies:
        return None, f"ordinal {ordinal} is not generated"

    def rendered(name: str) -> list[str]:
        body = bodies[name]
        return _normalized_lines([body[0].replace(name, "FN", 1), *body[1:]])

    a, b = rendered(orig_name), rendered(mutant_name)
    out: list[str] = []
    ctx = FINGERPRINT_CONTEXT
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, a, b, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        out.append(f"@{tag}")
        out += [f"-{ln}" for ln in a[max(0, i1 - ctx) : i1]]
        out += [f"<{ln}" for ln in a[i1:i2]]
        out += [f">{ln}" for ln in b[j1:j2]]
        out += [f"+{ln}" for ln in a[i2 : i2 + ctx]]
    if not out:
        return None, "normalized delta is empty"
    return out, None


def mutant_deltas(
    mutant_ids: "list[str]", root: str
) -> "tuple[dict[str, list[str]], dict[str, str]]":
    """REQ-B-4 normalized delta emissions, `({id: emission}, {id: error})`.

    Each generated module is read and parsed at most once. Every failure lands
    in the error map — an unresolvable ID never silently yields an empty delta.
    """
    deltas: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    cache: dict[str, tuple[dict[str, list[str]] | None, str | None]] = {}
    for mutant_id in mutant_ids:
        match = _MUTANT_ID_RE.match(mutant_id.strip())
        if match is None:
            errors[mutant_id] = f"unparseable mutant ID: {mutant_id!r}"
            continue
        module, mangled, ordinal = match.group("module", "mangled", "ordinal")
        if module not in cache:
            cache[module] = _function_bodies(module, root)
        bodies, error = cache[module]
        if bodies is None:
            errors[mutant_id] = f"{mutant_id}: {error}"
            continue
        emission, error = _emission(bodies, mangled, ordinal)
        if emission is None:
            errors[mutant_id] = f"{mutant_id}: {error}"
        else:
            deltas[mutant_id] = emission
    return deltas, errors


def fingerprints(
    mutant_ids: "list[str]", root: str
) -> "tuple[dict[str, str], dict[str, str]]":
    """REQ-B-4 fingerprints, `({id: 12-hex}, {id: error})`."""
    deltas, errors = mutant_deltas(mutant_ids, root)
    return {
        mid: hashlib.sha256("\n".join(emission).encode()).hexdigest()[:12]
        for mid, emission in deltas.items()
    }, errors


def fingerprint(mutant_id: str, root: str) -> "tuple[str | None, str | None]":
    """Single-ID form of `fingerprints`."""
    found, errors = fingerprints([mutant_id], root)
    return found.get(mutant_id), errors.get(mutant_id)


def mutant_delta(mutant_id: str, root: str) -> "tuple[list[str] | None, str | None]":
    """Single-ID form of `mutant_deltas`."""
    found, errors = mutant_deltas([mutant_id], root)
    return found.get(mutant_id), errors.get(mutant_id)
