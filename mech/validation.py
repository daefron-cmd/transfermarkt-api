"""Registration validation — anti-silent-unarmed (spec REQ-VAL-1/2).

Two independent checks over the hooks block, deliberately non-overlapping so
a single-field mutation produces exactly one error (cross-review B-23):
- `_check_command` genuinely imports whatever module a command references
  and confirms a callable `main` — this is what actually catches a typo'd
  module name or a real-but-wrong module (REQ-VAL-1).
- the hooks skeleton (event/matcher/type/module/timeout) is compared for
  exact set equality against the declarative `EXPECTED_SKELETON` (REQ-VAL-2).
  Entries whose command already failed the import/callable check are
  excluded from this comparison (`accounted_structures`) so the two checks
  never both fire for the same mutated entry.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable

from mech import paths, selftest

# Every git hook that must run the commit gate. `git commit` and `git commit
# --amend` fire pre-commit; a clean auto-merge fires pre-merge-commit INSTEAD,
# so pre-commit alone left merge commits uncertified (2026-07-27). Note what is
# still absent by necessity: git runs no pre-commit-equivalent hook for
# cherry-pick or revert, so those commits cannot be gated locally at all and
# rely on push CI — see BACKLOG.
GATE_HOOKS = ("pre-commit", "pre-merge-commit")

KNOWN_EVENTS = {
    "PermissionRequest",
    "PostCompact",
    "PostToolUse",
    "PreCompact",
    "PreToolUse",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "UserPromptSubmit",
}
MODULE_RE = re.compile(r"-m\s+(mech\.[A-Za-z0-9_.]+)")


# REQ-VAL-2 registration skeleton, normative: (event, matcher, hook type,
# module, timeout). `matcher` is None for events with no matcher (Stop).
#
# One entry since 2026-07-25. The PreToolUse edit/bash guards were retired with
# the escalation protocol, and Tier 1 moved to a global ruff-only hook outside
# any repo — so a repo-level PostToolUse registration is now the wrong shape to
# demand. See the spec's 2026-07-25 amendment.
# `[tool.mech] stop_hook = false` declines the Stop hook. It is report-only and
# runs the WHOLE suite after every turn, which on a repo whose suite takes
# minutes is a cost per interaction rather than a safety net — bayes measured
# 114 s. Before 2026-07-28 the only way to decline was to fail this module, and
# since `ci-validation` is `ci_push`'s first short-circuiting step, declining
# bricked push CI on an otherwise green repo. Default stays required: a repo
# that merely forgot to register it is still a finding.
def stop_hook_required(pyproject_text: str) -> bool:
    """Whether `[tool.mech]` declines the Stop hook. Absent or malformed ⇒ True.

    Parsed as TOML, not searched as text: `[tool.mech]` inside a string or
    comment is data, and must not select the text after it (panel 2026-08-02).
    Only a boolean `false` at `tool.mech.stop_hook` waives.
    """
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return True
    tool = data.get("tool")
    mech = tool.get("mech") if isinstance(tool, dict) else None
    value = mech.get("stop_hook") if isinstance(mech, dict) else None
    return value is not False


EXPECTED_SKELETON = frozenset(
    {
        ("Stop", None, "command", "mech.hook_stop", 300),
    }
)

# CI entry points (REQ-CI-0), invoked by workflows, never registered in
# hooks.json. Only checked once a committed workflow actually references
# them — see `_discover_ci_modules`.
CI_MODULE_RE = MODULE_RE

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def _sort_key(record: tuple) -> tuple:
    """Total-order sort key for skeleton tuples that may contain `None`
    (Stop's matcher) — plain `sorted()` would raise comparing `None` to
    `str` if two differing records ever shared a leading field."""
    return tuple("" if field is None else str(field) for field in record)


def _check_command(command: str, errors: list[str]) -> None:
    match = MODULE_RE.search(command)
    if not match:
        errors.append(f"command does not invoke a mech module: {command!r}")
        return
    module_name = match.group(1)
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        errors.append(f"registered module does not import: {module_name}")
        return
    if not callable(getattr(module, "main", None)):
        errors.append(f"registered module has no callable main(): {module_name}")


def _discover_ci_modules(root: str) -> list[str]:
    """REQ-CI-0: CI module names come from scanning the committed workflow
    YAML for `python -m mech.<name>`, never hardcoded. `.github/workflows/`
    does not exist until Task 15 — absence yields no candidates, not an
    error (nothing to check yet)."""
    workflows_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(workflows_dir):
        return []
    modules: set[str] = set()
    for name in sorted(os.listdir(workflows_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflows_dir, name), encoding="utf-8") as fh:
            modules.update(CI_MODULE_RE.findall(fh.read()))
    return sorted(modules)


def _hooks_errors(hooks: dict, expected: "frozenset | None" = None) -> list[str]:
    # Resolved at call time, not bound as a default: a default argument
    # captures EXPECTED_SKELETON at def time, which silently defeats the
    # monkeypatching the sort-key tests rely on.
    if expected is None:
        expected = EXPECTED_SKELETON
    errors: list[str] = []
    actual_skeleton: set[tuple] = set()
    accounted_structures: set[tuple] = set()
    for event, groups in hooks.items():
        for group in groups:
            matcher = group.get("matcher")
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                hook_type = hook.get("type")
                timeout = hook.get("timeout")
                struct_key = (event, matcher, hook_type, timeout)
                pre_count = len(errors)
                _check_command(command, errors)
                if len(errors) > pre_count:
                    # Already explained by a real import/callable failure —
                    # don't also report it as a generic skeleton mismatch.
                    accounted_structures.add(struct_key)
                    continue
                match = MODULE_RE.search(command)
                module_name = match.group(1) if match else command
                actual_skeleton.add((event, matcher, hook_type, module_name, timeout))

    if actual_skeleton != expected:
        missing = {
            record
            for record in expected - actual_skeleton
            if (record[0], record[1], record[2], record[4]) not in accounted_structures
        }
        extra = actual_skeleton - expected
        if missing or extra:
            unrecognized = sorted(
                {record[0] for record in extra if record[0] not in KNOWN_EVENTS}
            )
            message = (
                "hooks registration skeleton mismatch: "
                f"missing={sorted(missing, key=_sort_key)}, "
                f"unexpected={sorted(extra, key=_sort_key)}"
            )
            if unrecognized:
                message += f"; unrecognized hook event(s): {unrecognized}"
            errors.append(message)
    return errors


def validate(
    hooks_json: str,
    precommit_text: str,
    effective_hookspath: "str | None",
    in_ci: bool,
    stop_hook_required: bool = True,
) -> list[str]:
    errors: list[str] = []
    try:
        settings = json.loads(hooks_json)
    except ValueError as exc:
        return [f"hooks.json unparseable: {exc}"]

    expected = EXPECTED_SKELETON if stop_hook_required else frozenset()
    errors.extend(_hooks_errors(settings.get("hooks", {}), expected))

    # No permissions.deny check since 2026-07-25: gate config is governed by
    # convention now, so a deny list is neither required nor forbidden here.

    _check_command(precommit_text, errors)

    root = paths.repo_root(os.getcwd()) or "."
    for ci_module in _discover_ci_modules(root):
        # REQ-CI-0/REQ-VAL-1: a workflow-referenced mech.ci_* module that
        # cannot be imported is a hard failure — same silent-unarmed risk as
        # any other registered command (Task 14 unconditional; Task 13 had
        # this guarded behind existence since mech.ci_push/ci_weekly did not
        # exist yet).
        _check_command(f"uv run python -m {ci_module}", errors)

    if not in_ci and effective_hookspath != ".githooks":
        errors.append(
            f"core.hooksPath is {effective_hookspath!r}, expected '.githooks' "
            "— this clone is UNARMED (run: git config core.hooksPath .githooks)"
        )
    return errors


def is_ci(value: "str | None") -> bool:
    """Whether `CI=<value>` means we are running in CI.

    Reads the value rather than its truthiness. `bool("false")` is True, so
    until 2026-07-28 an exported `CI=false` — which toolchains use to suppress
    other tools' CI heuristics — made this module skip the `core.hooksPath`
    check, silencing the one check whose whole purpose is catching a silently
    unarmed clone.
    """
    return (value or "").strip().lower() not in ("", "false", "0")


def compatibility_record_errors(root: str, pyproject_text: str) -> list[str]:
    try:
        record = selftest.configured_record(pyproject_text)
    except selftest.RecordConfigError as exc:
        return [f"Pawl self-test configuration invalid: {exc}"]
    if record is None:
        return []
    error = selftest.record_error(root, record)
    return [error] if error is not None else []


def hook_index_mode_errors(root: str, run: Run = subprocess.run) -> list[str]:
    """REQ-VAL-1: every hook in GATE_HOOKS must carry 100755 in the git index.

    Checked per hook rather than for pre-commit alone since 2026-07-27. A hook
    that is committed non-executable is armed on paper and inert in practice,
    and the answer has to name which one — a pooled verdict sends you to the
    wrong file.
    """
    errors: list[str] = []
    for hook in GATE_HOOKS:
        rel = f".githooks/{hook}"
        proc = run(
            ["git", "ls-files", "--stage", rel],  # noqa: S607  # PATH-resolved `git` is intentional; matches paths.repo_root's convention
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if not proc.stdout.startswith("100755"):
            errors.append(f"{rel} index mode is not 100755: {proc.stdout.strip()!r}")
    return errors


def hook_worktree_executable_errors(root: str) -> list[str]:
    """REQ-VAL-1: every hook in GATE_HOOKS must be executable ON DISK.

    `hook_index_mode_errors` above checks the git INDEX. git obeys neither the
    index nor `core.fileMode` when deciding whether to run a hook — it stats
    the worktree file, and silently skips a non-executable one with a hint on
    stderr that scrolls past in any GUI client. Measured 2026-08-02 on a real
    clone: index mode 100755, `core.fileMode=false`, `chmod -x` the hook, and
    the commit lands ungated while every check here passed. The two are
    genuinely independent — a `core.fileMode=false` clone, a umask that strips
    +x at checkout, or a filesystem without exec bits all produce it — so this
    is a second check rather than a replacement for the first.
    """
    errors: list[str] = []
    for hook in GATE_HOOKS:
        path = os.path.join(root, ".githooks", hook)
        if not os.access(path, os.X_OK):
            errors.append(
                f".githooks/{hook} is not executable in the working tree — git "
                "SKIPS a non-executable hook, so this clone is UNARMED for that "
                f"operation (run: chmod +x .githooks/{hook})"
            )
    return errors


def main() -> int:
    cwd = os.getcwd()
    resolved_repository_root = paths.repo_root(cwd)
    repository_root = resolved_repository_root or "."
    project_root = (
        paths.project_root(cwd, resolved_repository_root)
        if resolved_repository_root is not None
        else "."
    )
    try:
        with open(
            os.path.join(repository_root, ".codex", "hooks.json"), encoding="utf-8"
        ) as fh:
            hooks_json = fh.read()
        hook_texts = {}
        for hook in GATE_HOOKS:
            with open(
                os.path.join(repository_root, ".githooks", hook), encoding="utf-8"
            ) as fh:
                hook_texts[hook] = fh.read()
    except OSError as exc:
        print(f"validation: missing registration file: {exc}", file=sys.stderr)
        return 1
    proc = subprocess.run(
        ["git", "config", "core.hooksPath"],  # noqa: S607  # PATH-resolved `git` is intentional; matches paths.repo_root's convention
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    hookspath = proc.stdout.strip() or None
    try:
        with open(os.path.join(project_root, "pyproject.toml"), encoding="utf-8") as fh:
            pyproject_text = fh.read()
    except OSError:
        pyproject_text = ""  # no pyproject ⇒ no waiver ⇒ Stop hook required
    errors = validate(
        hooks_json,
        hook_texts["pre-commit"],
        hookspath,
        in_ci=is_ci(os.environ.get("CI")),
        stop_hook_required=stop_hook_required(pyproject_text),
    )
    # validate() ran _check_command over the pre-commit text it was handed;
    # every other gate hook gets the same check, or a pre-merge-commit that
    # never invokes the gate would pass while looking armed.
    for hook, text in hook_texts.items():
        if hook != "pre-commit":
            _check_command(text, errors)
    errors.extend(hook_index_mode_errors(repository_root))
    errors.extend(hook_worktree_executable_errors(repository_root))
    errors.extend(compatibility_record_errors(project_root, pyproject_text))
    for error in errors:
        print(f"validation: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
