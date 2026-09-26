import json
import os
import subprocess

import pytest

from mech import selftest, validation

GOOD_SETTINGS = json.dumps(
    {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv run python -m mech.hook_stop",
                            "timeout": 300,
                        }
                    ]
                },
            ],
        },
    }
)
GOOD_PRECOMMIT = "#!/bin/sh\nexec uv run python -m mech.commit_gate\n"


def _write_gate_hooks(githooks_dir) -> None:
    """main() requires every hook in GATE_HOOKS to exist, so fixtures that
    exercise it write the whole set rather than pre-commit alone.

    Written 0o755 because that is what an armed clone actually looks like:
    git skips a non-executable hook, so a fixture without the bit models a
    silently unarmed repo, not a healthy one (2026-08-02)."""
    for hook in validation.GATE_HOOKS:
        path = githooks_dir / hook
        path.write_text(GOOD_PRECOMMIT, encoding="utf-8")
        path.chmod(0o755)


def test_repo_arms_a_gate_hook_for_every_hooked_commit_path():
    """`git commit` and `--amend` fire pre-commit, but a clean auto-merge fires
    pre-merge-commit instead. Shipping only pre-commit left merge commits
    ungated while the clone still reported itself armed."""
    root = validation.paths.repo_root(os.getcwd()) or "."
    for hook in validation.GATE_HOOKS:
        path = os.path.join(root, ".githooks", hook)
        assert os.path.exists(path), f"{hook} is not shipped"
        with open(path, encoding="utf-8") as fh:
            assert "mech.commit_gate" in fh.read(), f"{hook} does not run the gate"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("TRUE", True),
        # `bool("false")` is True, so these three used to read as "running in
        # CI" and skipped the arming check — in the exact scenario it exists
        # for. Toolchains export CI=false to suppress other tools' CI
        # heuristics, so this is a value that really occurs.
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("", False),
        (None, False),
    ],
)
def test_ci_flag_reads_the_value_not_its_truthiness(value, expected):
    assert validation.is_ci(value) is expected


def test_ci_false_still_enforces_hookspath(monkeypatch, tmp_path, capsys):
    """End of the same wire: an unarmed clone under CI=false must still be
    reported, or the one check whose purpose is catching a silently unarmed
    clone goes silent itself."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.setenv("CI", "false")

    def fake_run(argv, **kwargs):
        # an UNARMED clone: hooksPath is not .githooks
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    assert validation.main() == 1
    assert "UNARMED" in capsys.readouterr().err


EMPTY_HOOKS = json.dumps({"hooks": {}})


def test_stop_hook_is_required_by_default():
    """The default must stay fail-closed: a repo that simply forgot to register
    the hook is still a finding."""
    assert validation.stop_hook_required("") is True
    errors = validation.validate(EMPTY_HOOKS, GOOD_PRECOMMIT, ".githooks", in_ci=False)
    assert len(errors) == 1
    assert "mech.hook_stop" in errors[0]


def test_stop_hook_can_be_waived_by_committed_config():
    """The Stop hook runs the whole suite after every turn. On a repo whose
    suite takes minutes that is not a tradeoff anyone accepts, and before
    2026-07-28 the only way to decline it was to fail `mech.validation` — which
    is `ci_push`'s first, short-circuiting step, so declining bricked push CI on
    an otherwise green repo."""
    assert validation.stop_hook_required("[tool.mech]\nstop_hook = false\n") is False
    errors = validation.validate(
        EMPTY_HOOKS,
        GOOD_PRECOMMIT,
        ".githooks",
        in_ci=False,
        stop_hook_required=False,
    )
    assert errors == []


def test_waiving_the_stop_hook_does_not_stop_flagging_unexpected_hooks():
    """The waiver removes one expectation, not the drift check itself —
    otherwise it becomes a way to register anything unnoticed."""
    errors = validation.validate(
        GOOD_SETTINGS,  # a Stop hook IS registered, but config says waived
        GOOD_PRECOMMIT,
        ".githooks",
        in_ci=False,
        stop_hook_required=False,
    )
    assert len(errors) == 1
    assert "unexpected" in errors[0]


def test_stop_hook_flag_is_read_from_tool_mech_only():
    """A stray `stop_hook = false` elsewhere in pyproject must not disarm it."""
    assert validation.stop_hook_required("[tool.other]\nstop_hook = false\n") is True
    assert validation.stop_hook_required("[tool.mech]\nstop_hook = true\n") is True


def test_tool_mech_as_data_is_not_a_waiver():
    """`[tool.mech]` inside a TOML string or comment is data, not a table
    header. The text-split implementation selected whatever text followed it,
    so a repo with no `[tool.mech]` table at all could waive the Stop hook.
    Found by the 2026-08-02 most-defended panel (item 3)."""
    via_string = '[tool.other]\nnote = "[tool.mech]"\nstop_hook = false\n'
    assert validation.stop_hook_required(via_string) is True
    via_comment = "# [tool.mech]\nstop_hook = false\n"
    assert validation.stop_hook_required(via_comment) is True


def test_malformed_pyproject_keeps_the_stop_hook_required():
    """Unparseable TOML must fail closed — the docstring's contract is
    'absent or malformed ⇒ True'."""
    assert validation.stop_hook_required("[tool.mech\nstop_hook = false\n") is True
    assert validation.stop_hook_required('tool = "not a table"\n') is True


def test_a_later_section_cannot_disarm_the_stop_hook():
    """A real `[tool.mech]` table followed by a later section whose
    `stop_hook = false` must not count as mech's own.

    Originally pinned the text-split implementation's `^\\[` body truncation
    (two of its mutants survived the whole suite, found 2026-08-02 by
    mutation). The same day's most-defended panel replaced that
    implementation with `tomllib`; the behaviour this asserts is unchanged.
    """
    text = '[tool.mech]\npyright_scope = "whole"\n\n[tool.other]\nstop_hook = false\n'
    assert validation.stop_hook_required(text) is True


def _stop_hook_command(root):
    with open(os.path.join(root, ".codex", "hooks.json"), encoding="utf-8") as fh:
        return json.load(fh)["hooks"]["Stop"][0]["hooks"][0]["command"]


def _stop_hook_registered() -> bool:
    """Whether THIS repo actually registers a Stop hook.

    Not "does hooks.json exist": since `[tool.mech] stop_hook = false` a
    deployment may legitimately ship the file with an empty `hooks` block. The
    first version of this guard tested for the file and so ran the test against
    a waived deployment, where it died on a KeyError — caught by the live commit
    gate on the very adoption that introduced the waiver.
    """
    root = validation.paths.repo_root(os.getcwd()) or "."
    path = os.path.join(root, ".codex", "hooks.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            groups = json.load(fh).get("hooks", {}).get("Stop", [])
    except (OSError, ValueError):
        return False
    return any(hook.get("command") for g in groups for hook in g.get("hooks", []))


@pytest.mark.skipif(
    not _stop_hook_registered(),
    reason="no Stop hook registered here (unarmed, or waived via [tool.mech])",
)
def test_stop_hook_command_reaches_repo_root_from_a_subdirectory():
    """`mech` is not installed into the venv, so `python -m mech.hook_stop`
    resolves only from the project root. Codex runs hook commands with the
    session cwd, which can be a subdirectory, so the command first resolves the
    git root instead of assuming where the session started; a mixed deployment
    then supplies its project through `uv --directory`.

    Runs only the command's cd prefix, never the module — hook_stop runs pytest,
    so invoking it here would recurse into this suite.
    """
    cwd = os.getcwd()
    repository_root = validation.paths.repo_root(cwd) or "."
    project_root = validation.paths.project_root(cwd, repository_root)
    command = _stop_hook_command(repository_root)
    prefix, sep, _invocation = command.rpartition("&&")
    assert sep, f"Stop command has no prefix that places its cwd: {command!r}"

    proc = subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["sh", "-c", f"{prefix} && pwd"],  # noqa: S607  # PATH-resolved `sh` is intentional
        cwd=os.path.join(project_root, "mech"),
        capture_output=True,
        text=True,
        check=False,
        env=os.environ,
    )
    assert proc.returncode == 0, proc.stderr
    assert os.path.realpath(proc.stdout.strip()) == os.path.realpath(repository_root)


def test_valid_registration_passes():  # B-23
    assert (
        validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False)
        == []
    )


def test_malformed_hooks_json_returns_only_the_parse_error():
    """A hooks.json that is not JSON must short-circuit with one parse
    error. Falling through would call .get on a non-dict and raise, turning a
    readable config complaint into a traceback from a CI step."""
    errors = validation.validate("{ not json", GOOD_PRECOMMIT, ".githooks", in_ci=False)
    assert len(errors) == 1
    assert errors[0].startswith("hooks.json unparseable: ")


def test_typoed_module_fails():  # B-38
    bad = GOOD_SETTINGS.replace("mech.hook_stop", "mech.hook_stpo")
    assert any(
        "mech.hook_stpo" in e
        for e in validation.validate(bad, GOOD_PRECOMMIT, ".githooks", in_ci=False)
    )


def test_unknown_event_fails():  # B-23
    bad = GOOD_SETTINGS.replace("Stop", "Stopped")
    assert validation.validate(bad, GOOD_PRECOMMIT, ".githooks", in_ci=False)


def test_unexpected_hook_registration_fails():  # B-23
    """A registration nobody asked for is drift in the other direction — the
    guards were retired on 2026-07-25 and the modules deleted, so re-adding one
    is a finding twice over (skeleton drift, and an unimportable module)."""
    bad = json.loads(GOOD_SETTINGS)
    bad["hooks"]["PreToolUse"] = [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "uv run python -m mech.hook_bash_guard",
                    "timeout": 10,
                }
            ],
        }
    ]
    assert validation.validate(
        json.dumps(bad), GOOD_PRECOMMIT, ".githooks", in_ci=False
    )


def test_hookspath_checked_locally_not_ci():  # B-32
    assert validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, None, in_ci=False)
    assert validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, None, in_ci=True) == []


# --- Cross-review additions (mandatory, test-first) ---


def _mutate(mutator):
    """Deep-copy GOOD_SETTINGS, apply a single in-place mutation, re-serialize."""
    settings = json.loads(GOOD_SETTINGS)
    mutator(settings)
    return json.dumps(settings)


def _rename_event(settings, old, new):
    settings["hooks"] = {
        (new if k == old else k): v for k, v in settings["hooks"].items()
    }


SCHEMA_MUTATION_CASES = [
    pytest.param(
        lambda s: s["hooks"]["Stop"][0]["hooks"][0].__setitem__("timeout", 299),
        id="wrong_stop_timeout",
    ),
    pytest.param(
        # Stop takes no matcher; one present makes the tuple a mismatch.
        lambda s: s["hooks"]["Stop"][0].__setitem__("matcher", "Edit|Write"),
        id="unexpected_matcher",
    ),
    pytest.param(
        lambda s: s["hooks"]["Stop"][0]["hooks"][0].__setitem__("type", "script"),
        id="wrong_hook_type",
    ),
    pytest.param(
        lambda s: _rename_event(s, "Stop", "Stopped"),
        id="unknown_event",
    ),
    pytest.param(
        lambda s: s["hooks"]["Stop"][0]["hooks"][0].__setitem__(
            "command", "uv run python -m mech.hook_stpo"
        ),
        id="typoed_module",
    ),
    pytest.param(
        # mech.paths is a real, importable mech module with no callable main().
        lambda s: s["hooks"]["Stop"][0]["hooks"][0].__setitem__(
            "command", "uv run python -m mech.paths"
        ),
        id="missing_callable",
    ),
]


@pytest.mark.parametrize("mutator", SCHEMA_MUTATION_CASES)
def test_single_mutation_schema_case_produces_exactly_one_error(
    mutator,
):  # B-23 cross-review
    bad = _mutate(mutator)
    errors = validation.validate(bad, GOOD_PRECOMMIT, ".githooks", in_ci=False)
    assert len(errors) == 1, errors


def test_hook_index_mode_errors_flags_non_executable_index_mode():  # cross-review
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"100644 abc123def 0\t{argv[-1]}\n", stderr=""
        )

    errors = validation.hook_index_mode_errors("/repo", run=fake_run)
    assert len(errors) == len(validation.GATE_HOOKS)
    assert all("100644" in error for error in errors)


def test_hook_index_mode_errors_names_only_the_non_executable_hook():
    """A per-hook check has to say WHICH hook is unarmed — a single pooled
    verdict sent you to the wrong file."""

    def fake_run(argv, **kwargs):
        mode = "100644" if argv[-1].endswith("pre-merge-commit") else "100755"
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"{mode} abc123def 0\t{argv[-1]}\n", stderr=""
        )

    errors = validation.hook_index_mode_errors("/repo", run=fake_run)
    assert len(errors) == 1
    assert "pre-merge-commit" in errors[0]


def test_hook_index_mode_errors_100755_passes():  # cross-review
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"100755 abc123def 0\t{argv[-1]}\n", stderr=""
        )

    assert validation.hook_index_mode_errors("/repo", run=fake_run) == []


def test_hook_worktree_executable_errors_flags_a_non_executable_checkout(tmp_path):
    """git obeys the WORKTREE bit, not the index. Measured 2026-08-02: with
    index mode 100755 and `core.fileMode=false`, `chmod -x` on the hook makes
    git print "hook was ignored because it's not set as executable" and the
    commit lands — the exact "armed on paper, inert in practice" state the
    index-mode check exists to catch, on the side git actually reads."""
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    for hook in validation.GATE_HOOKS:
        path = hooks / hook
        path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
    assert validation.hook_worktree_executable_errors(str(tmp_path)) == []

    (hooks / "pre-merge-commit").chmod(0o644)
    errors = validation.hook_worktree_executable_errors(str(tmp_path))
    assert errors == [
        ".githooks/pre-merge-commit is not executable in the working tree — git "
        "SKIPS a non-executable hook, so this clone is UNARMED for that "
        "operation (run: chmod +x .githooks/pre-merge-commit)"
    ]


def test_hook_worktree_executable_errors_reports_an_absent_hook(tmp_path):
    (tmp_path / ".githooks").mkdir()
    errors = validation.hook_worktree_executable_errors(str(tmp_path))
    assert len(errors) == len(validation.GATE_HOOKS)


def test_main_wires_hook_worktree_executable_errors(tmp_path, monkeypatch, capsys):
    """The check is worthless unless main actually runs it."""
    monkeypatch.setattr(validation.paths, "repo_root", lambda _cwd: str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text(GOOD_SETTINGS)
    (tmp_path / ".githooks").mkdir()
    for hook in validation.GATE_HOOKS:
        (tmp_path / ".githooks" / hook).write_text(GOOD_PRECOMMIT)
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stub discards root
    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(
        validation,
        "hook_worktree_executable_errors",
        lambda root: ["worktree exec boom"],  # noqa: ARG005  # stub discards root
    )

    assert validation.main() == 1
    assert "worktree exec boom" in capsys.readouterr().err


def test_main_wires_hook_index_mode_errors(
    tmp_path, monkeypatch, capsys
):  # cross-review
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(
        validation, "hook_index_mode_errors", lambda root: ["index mode boom"]
    )  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.delenv("CI", raising=False)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=".githooks\n", stderr="")

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    rc = validation.main()

    assert rc == 1
    assert "index mode boom" in capsys.readouterr().err


def test_main_passes_when_registration_is_fully_valid(
    tmp_path, monkeypatch
):  # cross-review
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.delenv("CI", raising=False)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=".githooks\n", stderr="")

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    assert validation.main() == 0


def _main_fixture(tmp_path, monkeypatch, precommit: str, premerge: str):
    """Drive `main()` against a tmp repo with the two gate hooks written
    independently, so a hook can be broken one at a time."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    (githooks_dir / "pre-commit").write_text(precommit, encoding="utf-8")
    (githooks_dir / "pre-merge-commit").write_text(premerge, encoding="utf-8")
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(  # noqa: ARG005  # stub must accept the kwargs it discards
            argv, 0, stdout=".githooks\n", stderr=""
        ),
    )


BAD_HOOK = "#!/bin/sh\nexec uv run python -m mech.no_such_module\n"


def test_a_broken_pre_merge_commit_command_is_a_finding(tmp_path, monkeypatch, capsys):
    """The 2026-07-27 fix added pre-merge-commit because a clean auto-merge
    fires it instead of pre-commit. `validate()` only ever sees the pre-commit
    text, so `main()` re-runs `_check_command` over every *other* gate hook —
    without that loop a pre-merge-commit invoking nothing would pass while
    looking armed, and merge commits would go uncertified.

    Found 2026-08-02 by mutation: inverting that loop's condition, and passing
    it `None` instead of the error list, both survived the whole suite. Nothing
    had ever given pre-merge-commit a command that fails to import — the
    existing pre-merge-commit test covers its index mode, not its contents.
    """
    _main_fixture(tmp_path, monkeypatch, precommit=GOOD_PRECOMMIT, premerge=BAD_HOOK)
    assert validation.main() == 1
    assert "mech.no_such_module" in capsys.readouterr().err


def test_pre_commit_is_not_command_checked_twice(tmp_path, monkeypatch, capsys):
    """`validate()` already checked pre-commit, so `main()`'s loop must skip it.
    A duplicate check is invisible on a healthy repo and doubles every finding
    on a broken one, which is where the operator is already reading carefully.

    Found 2026-08-02 by mutation: blanking and upper-casing the `"pre-commit"`
    literal in that condition both survived, because every existing fixture
    passes a pre-commit that imports cleanly.
    """
    _main_fixture(tmp_path, monkeypatch, precommit=BAD_HOOK, premerge=GOOD_PRECOMMIT)
    assert validation.main() == 1
    assert capsys.readouterr().err.count("mech.no_such_module") == 1


def test_main_missing_registration_file_fails(tmp_path, monkeypatch):  # cross-review
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    assert validation.main() == 1


def test_ci_modules_absent_workflows_dir_is_no_error():  # cross-review REQ-CI-0
    # .github/workflows/ does not exist yet (created in Task 15); validate()
    # must not error just because there is nothing to scan yet.
    assert (
        validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False)
        == []
    )


# --- Mutation-hardening (dogfood: killing real mutmut survivors) ---


def test_sort_key_replaces_none_with_empty_string():
    assert validation._sort_key(("Stop", None, "command", "mech.hook_stop", 300)) == (
        "Stop",
        "",
        "command",
        "mech.hook_stop",
        "300",
    )


def test_check_command_no_module_invoked_exact_message():
    errors = []
    validation._check_command("echo hi", errors)
    assert errors == ["command does not invoke a mech module: 'echo hi'"]


def test_check_command_import_error_exact_message():
    errors = []
    validation._check_command("uv run python -m mech.nonexistent_module_xyz", errors)
    assert errors == ["registered module does not import: mech.nonexistent_module_xyz"]


def test_check_command_missing_callable_exact_message():
    errors = []
    validation._check_command("uv run python -m mech.paths", errors)
    assert errors == ["registered module has no callable main(): mech.paths"]


def test_check_command_valid_module_no_errors():
    errors = []
    validation._check_command("uv run python -m mech.hook_stop", errors)
    assert errors == []


def test_discover_ci_modules_finds_module_reference(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "push.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - run: uv run python -m mech.ci_push\n",
        encoding="utf-8",
    )
    assert validation._discover_ci_modules(str(tmp_path)) == ["mech.ci_push"]


def test_discover_ci_modules_dedupes_sorts_and_reads_yaml_extension(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "weekly.yaml").write_text(
        "run: uv run python -m mech.ci_weekly\n", encoding="utf-8"
    )
    (workflows / "push.yml").write_text(
        "run: uv run python -m mech.ci_push\nrun: uv run python -m mech.ci_push\n",
        encoding="utf-8",
    )
    assert validation._discover_ci_modules(str(tmp_path)) == [
        "mech.ci_push",
        "mech.ci_weekly",
    ]


def test_discover_ci_modules_ignores_non_yaml_files(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "README.md").write_text(
        "uv run python -m mech.ci_push\n", encoding="utf-8"
    )
    assert validation._discover_ci_modules(str(tmp_path)) == []


def test_discover_ci_modules_skips_non_yaml_then_keeps_scanning(tmp_path):
    # Guards `continue` (not `break`) on the non-yaml skip: a later, real
    # workflow file must still be scanned.
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "a-readme.md").write_text("not yaml", encoding="utf-8")
    (workflows / "z-push.yml").write_text(
        "run: uv run python -m mech.ci_push\n", encoding="utf-8"
    )
    assert validation._discover_ci_modules(str(tmp_path)) == ["mech.ci_push"]


def test_hooks_errors_group_missing_hooks_key_does_not_crash():
    errors = validation._hooks_errors({"Stop": [{}]})
    assert errors  # Stop registration absent -> reported, not a crash


def test_hooks_errors_hook_missing_command_key_exact_message():
    hooks = {"Stop": [{"hooks": [{"type": "command", "timeout": 300}]}]}
    errors = validation._hooks_errors(hooks)
    assert "command does not invoke a mech module: ''" in errors


def test_hooks_errors_valid_but_wrong_field_reported_as_extra_not_swallowed():
    # B-23: a structurally-valid, importable command whose fields don't match
    # any EXPECTED_SKELETON row must show up as "unexpected", not vanish —
    # guards the `>` (not `>=`) comparison against pre_count/post_count.
    hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_stop",
                        "timeout": 12345,
                    }
                ]
            }
        ]
    }
    errors = validation._hooks_errors(hooks)
    assert len(errors) == 1
    assert "12345" in errors[0]


def test_hooks_errors_continues_to_next_entry_after_command_error():
    # Guards `continue` (not `break`) after a per-command error: a second
    # hook entry in the same group must still be evaluated.
    hooks = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_typo_xyz",
                        "timeout": 10,
                    },
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_stop",
                        "timeout": 999,
                    },
                ],
            }
        ]
    }
    errors = validation._hooks_errors(hooks)
    assert any("hook_typo_xyz" in e for e in errors)
    assert any("999" in e for e in errors)


def test_hooks_errors_unrecognized_uses_event_field_not_matcher_field():
    # Guards: the `unrecognized` set is built from `record[0]` (event) under
    # the `record[0] not in KNOWN_EVENTS` filter — not the matcher field, and
    # not an inverted `in` check. Craft a matcher string that itself equals a
    # known event name so record[0]/record[1] confusion is observable.
    hooks = {
        "Weirdo": [
            {
                "matcher": "PreToolUse",
                "hooks": [
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_stop",
                        "timeout": 1,
                    }
                ],
            }
        ]
    }
    errors = validation._hooks_errors(hooks)
    assert len(errors) == 1
    assert errors[0].startswith("hooks registration skeleton mismatch: ")
    assert "unrecognized hook event(s): ['Weirdo']" in errors[0]


def test_hooks_errors_sort_handles_none_matcher_among_string_matchers():
    # Guards the `key=_sort_key` on the `extra` sort: two extra entries under
    # the same event, one with a `None` matcher (Stop-shaped) and one with a
    # string matcher — plain tuple sort would raise comparing None to str.
    hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_stop",
                        "timeout": 999,
                    }
                ]
            },
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "uv run python -m mech.hook_stop",
                        "timeout": 998,
                    }
                ],
            },
        ]
    }
    errors = validation._hooks_errors(hooks)
    assert len(errors) == 1
    assert "998" in errors[0]
    assert "999" in errors[0]


def test_hooks_errors_sort_handles_none_matcher_among_string_matchers_missing(
    monkeypatch,
):
    # Mirror of the `extra`-branch test above, but for the `missing` sort:
    # guards the `key=_sort_key` on `sorted(missing, key=_sort_key)`. Patch
    # EXPECTED_SKELETON to two rows sharing an event, one with a `None`
    # matcher and one with a string matcher, then call with empty hooks so
    # both rows land entirely in `missing` (actual_skeleton is empty, nothing
    # is accounted-for). Plain tuple sort would raise TypeError comparing
    # None to str on the matcher field; real code's `_sort_key` coerces both
    # to str first and doesn't crash. Kills mutmut_65 (`key=None`) and
    # mutmut_67 (kwarg dropped entirely) on the `missing` sorted() call —
    # both were misfiled in MUTANTS.md as `equivalent` even though this
    # exact single-`None`-row assumption from the `_hooks_errors` docstring
    # reasoning doesn't hold once EXPECTED_SKELETON is patched.
    monkeypatch.setattr(
        validation,
        "EXPECTED_SKELETON",
        frozenset(
            {
                ("PreToolUse", None, "command", "mech.hook_a", 10),
                ("PreToolUse", "Bash", "command", "mech.hook_b", 20),
            }
        ),
    )
    errors = validation._hooks_errors({})
    assert len(errors) == 1
    assert "mech.hook_a" in errors[0]
    assert "mech.hook_b" in errors[0]


def test_validate_missing_hooks_key_does_not_crash():
    settings = json.loads(GOOD_SETTINGS)
    del settings["hooks"]
    errors = validation.validate(
        json.dumps(settings), GOOD_PRECOMMIT, ".githooks", in_ci=False
    )
    assert errors


def test_validate_reports_precommit_command_errors():
    bad_precommit = "#!/bin/sh\nexec uv run python -m mech.nonexistent_xyz\n"
    errors = validation.validate(GOOD_SETTINGS, bad_precommit, ".githooks", in_ci=False)
    assert any("mech.nonexistent_xyz" in e for e in errors)


def test_validate_checks_ci_modules_discovered_from_workflows(tmp_path, monkeypatch):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "push.yml").write_text(
        "run: uv run python -m mech.paths\n", encoding="utf-8"
    )  # real, no main()
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    errors = validation.validate(
        GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False
    )
    assert any("mech.paths" in e and "no callable main" in e for e in errors)


def test_validate_ci_module_loop_continues_after_failing_module(
    tmp_path, monkeypatch
):  # Task 14: the guard is now unconditional (was skip-if-missing in Task 13)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "a.yml").write_text(
        "run: uv run python -m mech.ci_totally_absent\n", encoding="utf-8"
    )
    (workflows / "b.yml").write_text(
        "run: uv run python -m mech.paths\n", encoding="utf-8"
    )
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    errors = validation.validate(
        GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False
    )
    assert any("mech.paths" in e and "no callable main" in e for e in errors)
    assert any("mech.ci_totally_absent" in e and "does not import" in e for e in errors)


def test_validation_fails_on_missing_ci_module(
    tmp_path, monkeypatch
):  # REQ-CI-0 unconditional (Task 14 cross-review)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "push.yml").write_text(
        "run: uv run python -m mech.ci_totally_absent\n", encoding="utf-8"
    )
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    errors = validation.validate(
        GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False
    )
    assert any("mech.ci_totally_absent" in e and "does not import" in e for e in errors)


def test_validate_falls_back_to_literal_dot_when_repo_root_is_falsy(monkeypatch):
    # No monkeypatch.chdir here: mutmut's own trampoline instrumentation
    # resolves `source_paths` relative to the *actual* process cwd at call
    # time, so changing it mid-test breaks mutation testing itself. Capture
    # the fallback value directly instead of exercising it via real I/O.
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: None)  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    received = []
    monkeypatch.setattr(
        validation, "_discover_ci_modules", lambda root: received.append(root) or []
    )
    validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, ".githooks", in_ci=False)
    assert received == ["."]


def test_hookspath_error_message_is_exact():
    errors = validation.validate(GOOD_SETTINGS, GOOD_PRECOMMIT, None, in_ci=False)
    assert errors == [
        "core.hooksPath is None, expected '.githooks' "
        "— this clone is UNARMED (run: git config core.hooksPath .githooks)"
    ]


def test_hook_index_mode_errors_calls_run_with_exact_args():
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="100755\n", stderr="")

    validation.hook_index_mode_errors("/repo", run=fake_run)
    assert calls == [
        (
            ["git", "ls-files", "--stage", f".githooks/{hook}"],
            {"cwd": "/repo", "capture_output": True, "text": True, "check": False},
        )
        for hook in validation.GATE_HOOKS
    ]


def test_main_missing_file_prints_exact_prefix_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    rc = validation.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("validation: missing registration file: ")


def test_compatibility_record_errors_are_opt_in_and_fail_on_staleness(tmp_path):
    assert (
        validation.compatibility_record_errors(
            str(tmp_path), '[tool.mech]\npyright_scope = "whole"\n'
        )
        == []
    )
    configured = '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n'
    assert validation.compatibility_record_errors(str(tmp_path), configured) == [
        "Pawl self-test record is missing: .pawl-selftest.json"
    ]
    escaped = '[tool.mech]\npawl_selftest_record = "../outside.json"\n'
    assert validation.compatibility_record_errors(str(tmp_path), escaped) == [
        "Pawl self-test configuration invalid: pawl_selftest_record must be a "
        "nonempty relative path inside the repository"
    ]


def test_main_calls_git_config_hookspath_with_exact_args(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.delenv("CI", raising=False)

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=".githooks\n", stderr="")

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    validation.main()

    assert calls == [
        (
            ["git", "config", "core.hooksPath"],
            {
                "cwd": str(tmp_path),
                "capture_output": True,
                "text": True,
                "check": False,
            },
        )
    ]


def test_main_passes_correct_root_to_hook_index_mode_errors(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    received = []
    monkeypatch.setattr(
        validation,
        "hook_index_mode_errors",
        lambda root: received.append(root) or [],
    )
    record_roots = []
    monkeypatch.setattr(
        validation,
        "compatibility_record_errors",
        lambda root, text: record_roots.append(root) or [],  # noqa: ARG005  # this test pins root propagation, not record parsing
    )
    monkeypatch.delenv("CI", raising=False)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=".githooks\n", stderr="")

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    validation.main()

    assert received == [str(tmp_path)]
    assert record_roots == [str(tmp_path)]


def test_main_in_ci_skips_hookspath_check(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.setenv("CI", "1")

    def fake_run(argv, **kwargs):
        # A hookspath that would fail the check if it were ever evaluated.
        return subprocess.CompletedProcess(
            argv, 0, stdout="/some/other/path\n", stderr=""
        )

    monkeypatch.setattr(validation.subprocess, "run", fake_run)

    assert validation.main() == 0


def test_main_falls_back_to_literal_dot_when_repo_root_is_falsy(monkeypatch):
    # No monkeypatch.chdir here (see test_validate_falls_back_to_literal_dot_
    # when_repo_root_is_falsy) — capture the path `main()` attempts to open
    # instead of exercising the fallback via real chdir'd I/O.
    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: None)  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    opened_paths = []

    def fake_open(path, *_args, **_kwargs):
        opened_paths.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)

    assert validation.main() == 1
    assert opened_paths == [os.path.join(".", ".codex", "hooks.json")]


def test_main_without_git_uses_literal_project_root_for_stop_waiver(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mech]\nstop_hook = false\n", encoding="utf-8"
    )
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "hooks.json").write_text(EMPTY_HOOKS, encoding="utf-8")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    _write_gate_hooks(githooks)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validation.paths, "repo_root", lambda _cwd: None)
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda _root: [])
    monkeypatch.setattr(validation, "hook_worktree_executable_errors", lambda _root: [])
    monkeypatch.setenv("CI", "1")

    assert validation.main() == 0
    assert capsys.readouterr().err == ""


def test_main_honours_a_waived_stop_hook(tmp_path, monkeypatch, capsys):
    """End of the wire: a repo that declines the Stop hook in committed config
    and registers none must pass, or ci-validation — ci_push's first,
    short-circuiting step — turns push CI red on an otherwise green repo."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mech]\nstop_hook = false\n", encoding="utf-8"
    )
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(EMPTY_HOOKS, encoding="utf-8")
    githooks_dir = tmp_path / ".githooks"
    githooks_dir.mkdir()
    _write_gate_hooks(githooks_dir)

    monkeypatch.setattr(validation.paths, "repo_root", lambda cwd: str(tmp_path))  # noqa: ARG005  # stubbed repo_root signature must accept the cwd arg it discards
    monkeypatch.setattr(validation, "hook_index_mode_errors", lambda root: [])  # noqa: ARG005  # stubbed hook_index_mode_errors signature must accept the root arg it discards
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, ".githooks\n", ""),
    )

    assert validation.main() == 0
    assert capsys.readouterr().err == ""


def test_main_checks_nested_project_record_against_repo_registration(
    tmp_path, monkeypatch, capsys
):
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved git; fixture only
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],  # noqa: S607  # fixture-only config
        cwd=tmp_path,
        check=True,
    )
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "hooks.json").write_text(GOOD_SETTINGS, encoding="utf-8")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    _write_gate_hooks(githooks)

    project = tmp_path / "service"
    mech = project / "mech"
    mech.mkdir(parents=True)
    gate_file = mech / "gate.py"
    gate_file.write_text("x = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n',
        encoding="utf-8",
    )
    selftest.write_record(str(project), ".pawl-selftest.json")
    gate_file.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"],  # noqa: S607  # fixture-only git staging
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("CI", "1")

    assert validation.main() == 1
    assert "Pawl self-test record is stale" in capsys.readouterr().err
