"""Push-CI entry: deterministic checks plus diff-scoped mutation.

`main` short-circuits its steps exactly like the commit gate (REQ-CG-3):
first non-pass step stops the run. Every tool subprocess invocation's
argv/timeout comes from the `tools.CI` table (REQ-ARCH-9) via the `run=` seam
threaded through every call; nothing is inlined in control flow.

Mutation returned to the push path on 2026-09-01, after Pawl's tests moved
behind the `mech/` ownership boundary. It deliberately did not return to the
latency-sensitive commit gate. Source changes mutate only mapped modules;
MUTANTS.md-only changes regenerate the tree and verify every row; unrelated
diffs skip the mutation setup entirely.
"""

import json
import os
import subprocess
import sys
from typing import cast

from mech import baseline, mutation, source_inventory, tools

ZERO_SHA = "0" * 40
FULL_MUTATION_INPUTS = frozenset({"pyproject.toml", "uv.lock"})

_CI: dict[str, tuple[tuple[str, ...], int]] = {
    slot: (argv, timeout) for slot, argv, timeout in tools.CI
}

_scrubbed_env = tools.scrubbed_env


def diff_base(event_before: "str | None", ref: str, default_branch: str) -> dict:
    """Choose the authoritative push diff base, failing toward more mutation."""
    branch = ref.removeprefix("refs/heads/")
    if branch == default_branch:
        if not event_before or event_before == ZERO_SHA:
            return {"mode": "unscoped", "base": None}
        return {"mode": "scoped", "base": event_before}
    return {"mode": "scoped", "base": "merge-base"}


def coverage_verdict(coverage_json: dict, source_modules: list[str]) -> list[str]:
    """REQ-CI-1b: a source module absent from the report counts as 0%
    (mutation-untestable); present-but-zero-statement modules are excluded."""
    files = coverage_json.get("files", {})
    absent = [m for m in source_modules if m not in files]
    present = [m for m in source_modules if m in files]
    uncovered = []
    for path in sorted(present):
        summary = files[path].get("summary", {})
        if (
            summary.get("num_statements", 0) > 0
            and summary.get("covered_lines", 0) == 0
        ):
            uncovered.append(path)
    return absent + uncovered


def _tool_step(
    argv: list[str],
    root: str,
    name: str,
    run,
    timeout: int,
    no_tests_is_error: bool = False,
) -> "str | None":
    """Reimplementation of gate._tool_step's shape (not imported — private).
    Returns error text or None; CI maps both fail and error to exit 1
    (REQ-ARCH-6 CI row), so no separate exception class is needed here."""
    try:
        proc = run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_scrubbed_env(),
        )
    except Exception as exc:  # noqa: BLE001  # fail-closed
        return f"{name} could not run: {type(exc).__name__}: {exc}"
    if no_tests_is_error and proc.returncode == 5:  # REQ-CG-3 step 4
        return f"{name} collected no tests — infrastructure error (fail-closed)"
    if proc.returncode != 0:
        tail = f"{proc.stdout[-3000:]}{proc.stderr[-1000:]}"
        return f"{name} failed:\n{tail}"
    return None


def _discover_source_modules(root: str) -> list[str]:
    return source_inventory.load(root).discover_python_files(root)


def _all_inventory_modules(
    inventory: source_inventory.SourceInventory, root: str
) -> list[str]:
    modules = {
        module
        for path in inventory.discover_python_files(root)
        if (module := inventory.module_for(path)) is not None
    }
    return sorted(modules)


def _git_call(argv: list[str], root: str, run, timeout: int, *, text: bool = True):
    try:
        return run(
            argv,
            cwd=root,
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
            env=_scrubbed_env(),
        ), None
    except Exception as exc:  # noqa: BLE001  # CI must fail closed
        return None, f"git diff discovery failed: {type(exc).__name__}: {exc}"


def _name_status_entries(
    output: object,
) -> "tuple[list[tuple[str, list[str]]], str | None]":
    """Parse byte-safe `git diff --name-status -z` output."""
    if not isinstance(output, bytes):
        return [], "git diff output was not bytes: fail closed"
    if not output:
        return [], None
    if not output.endswith(b"\0"):
        return [], "git diff output was not NUL-terminated: fail closed"
    fields = output[:-1].split(b"\0")

    entries: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(fields):
        status = os.fsdecode(fields[index])
        index += 1
        path_count = 2 if status.startswith("R") else 1
        if status not in {"A", "M"} and not status.startswith("R"):
            return [], f"git diff output has unexpected status {status!r}: fail closed"
        if index + path_count > len(fields):
            return (
                [],
                f"git diff output is truncated after status {status!r}: fail closed",
            )
        paths = [os.fsdecode(path) for path in fields[index : index + path_count]]
        index += path_count
        if any(not path for path in paths):
            return (
                [],
                f"git diff output has an empty path for status {status!r}: fail closed",
            )
        entries.append((status, paths))
    return entries, None


def _mutation_scope(
    event: dict,
    root: str,
    inventory: source_inventory.SourceInventory,
    run,
) -> "tuple[list[str], bool, bool, str | None]":
    """Return modules, baseline-changed, verify-all, and an optional error."""
    repository = event.get("repository")
    default_branch = (
        repository.get("default_branch", "main")
        if isinstance(repository, dict)
        else "main"
    )
    plan = diff_base(event.get("before"), event.get("ref", ""), default_branch)
    if plan["mode"] == "unscoped":
        try:
            return _all_inventory_modules(inventory, root), False, True, None
        except source_inventory.InventoryError as exc:
            return [], False, True, f"source inventory unreadable: {exc}"

    base = plan["base"]
    if base == "merge-base":
        argv_t, timeout = _CI["ci-git-merge-base"]
        argv = [arg.replace("{default_branch}", default_branch) for arg in argv_t]
        proc, error = _git_call(argv, root, run, timeout)
        if error is not None:
            return [], False, False, error
        proc = cast(subprocess.CompletedProcess, proc)  # pragma: no mutate
        if proc.returncode != 0 or not proc.stdout.strip():
            return [], False, False, "merge base unavailable: fail closed"
        base = proc.stdout.strip()

    diff_range = f"{base}...HEAD"
    argv_t, timeout = _CI["ci-git-diff"]
    argv = [arg.replace("{range}", diff_range) for arg in argv_t]
    proc, error = _git_call(argv, root, run, timeout, text=False)
    if error is not None:
        return [], False, False, error
    proc = cast(subprocess.CompletedProcess, proc)  # pragma: no mutate
    if proc.returncode != 0:
        return [], False, False, f"diff base unavailable ({diff_range}): fail closed"

    entries, error = _name_status_entries(proc.stdout)
    if error is not None:
        return [], False, False, error

    modules: set[str] = set()
    baseline_changed = False
    verify_all = False
    for status, changed_paths in entries:
        path = changed_paths[-1]
        if "MUTANTS.md" in changed_paths:
            baseline_changed = True
        if FULL_MUTATION_INPUTS.intersection(changed_paths):
            verify_all = True
        if inventory.eligible(path):
            module = inventory.module_for(path)
            if module is None:
                return (
                    [],
                    baseline_changed,
                    verify_all,
                    f"unmappable changed path: {path}",
                )
            modules.add(module)

    if verify_all:
        try:
            modules.update(_all_inventory_modules(inventory, root))
        except source_inventory.InventoryError as exc:
            return [], baseline_changed, True, f"source inventory unreadable: {exc}"
    return sorted(modules), baseline_changed, verify_all, None


def _read_baseline(root: str) -> "tuple[list[baseline.Row], str | None]":
    try:
        with open(os.path.join(root, "MUTANTS.md"), encoding="utf-8") as fh:
            rows = baseline.parse(fh.read())
    except (OSError, baseline.BaselineError) as exc:
        return [], f"MUTANTS.md unreadable: {type(exc).__name__}: {exc}"
    for row in rows:
        problem = baseline.validate_row(row)
        if problem is not None:
            return [], f"MUTANTS.md invalid row {row[0]}: {problem}"
    return rows, None


def _fingerprint_errors(rows: list[baseline.Row], root: str) -> list[str]:
    actual, errors = mutation.fingerprints([row[0] for row in rows], root)
    failures = []
    for row in rows:
        mutant_id, expected = row[0], row[4]
        observed = actual.get(mutant_id)
        if observed != expected:
            detail = errors.get(mutant_id, f"expected {expected}, got {observed}")
            failures.append(f"baseline fingerprint mismatch: {mutant_id}: {detail}")
    return failures


def main(root: "str | None" = None, run=subprocess.run) -> int:
    root = root or os.getcwd()
    failures: list[str] = []
    inventory = None

    for slot, no_tests_is_error in (
        ("ci-validation", False),
        ("ci-ruff-format", False),
        ("ci-ruff-check", False),
        ("ci-pyright", False),
        ("ci-coverage-run", True),
    ):
        argv, timeout = _CI[slot]
        error = _tool_step(list(argv), root, slot, run, timeout, no_tests_is_error)
        if error:
            failures.append(error)
            break  # short-circuit like the local gate (REQ-CG-3)

    if not failures:
        argv, timeout = _CI["ci-coverage-json"]
        proc = run(
            list(argv),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_scrubbed_env(),
        )
        if proc.returncode != 0:
            failures.append(
                f"ci-coverage-json failed:\n{proc.stdout[-3000:]}{proc.stderr[-1000:]}"
            )
        else:
            try:
                with open(
                    os.path.join(root, ".mech", "coverage.json"), encoding="utf-8"
                ) as fh:
                    coverage_json = json.load(fh)
            except (OSError, ValueError) as exc:
                failures.append(
                    f"coverage.json unreadable: {type(exc).__name__}: {exc}"
                )
            else:
                try:
                    inventory = source_inventory.load(root)
                    source_files = inventory.discover_python_files(root)
                except source_inventory.InventoryError as exc:
                    failures.append(f"source inventory unreadable: {exc}")
                else:
                    uncovered = coverage_verdict(coverage_json, source_files)
                    failures += [f"mutation-untestable: {m}" for m in uncovered]

    event = None
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not failures and event_path:
        try:
            with open(event_path, encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError) as exc:
            failures.append(
                f"GITHUB_EVENT_PATH unreadable: {type(exc).__name__}: {exc}"
            )

    if not failures and isinstance(event, dict) and inventory is not None:
        modules, baseline_changed, verify_all, error = _mutation_scope(
            event, root, inventory, run
        )
        if error is not None:
            failures.append(error)
        elif not modules and not baseline_changed:
            print("ci_push: mutation skipped (no relevant diff)")
        else:
            rows, error = _read_baseline(root)
            if error is not None:
                failures.append(error)
            else:
                error = mutation.xdist_config_error(root)
                if error is not None:
                    failures.append(error)
                else:
                    mutation_env = mutation.execution_env()
                    survivors: list[str] = []
                    if modules:
                        survivors, error = mutation.run_scoped(
                            modules, root, run=run, env=mutation_env
                        )
                        scope = "mutated " + ", ".join(modules)
                    else:
                        error = mutation.regenerate_tree(
                            root, run=run, env=mutation_env
                        )
                        scope = "tree regenerated"
                    if error is not None:
                        failures.append(error)
                    else:
                        checked_rows = (
                            rows
                            if baseline_changed or verify_all
                            else [
                                row
                                for row in rows
                                if any(
                                    row[0].startswith(f"{module}.")
                                    for module in modules
                                )
                            ]
                        )
                        failures += _fingerprint_errors(checked_rows, root)
                        baseline_ids = {row[0] for row in rows}
                        new_survivors = sorted(set(survivors) - baseline_ids)
                        failures += [f"new survivor: {mid}" for mid in new_survivors]
                        if not failures:
                            print(
                                f"ci_push: verified {len(checked_rows)} "
                                f"baseline row(s); {scope}; "
                                f"{len(new_survivors)} new survivor(s)"
                            )

    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
