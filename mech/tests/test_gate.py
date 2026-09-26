import builtins
import os
import shutil
import subprocess

import pytest

from mech import gate, imports_analyzer, paths, tools, verdict


def test_staging_warning_clean_returns_none():  # B-15
    assert gate.staging_warning("M  staged_only.py\n") is None
    assert gate.staging_warning("") is None


def _warn(status: str) -> str:
    msg = gate.staging_warning(status)
    assert msg is not None, f"expected a warning for {status!r}"
    return msg


def test_staging_warning_reports_unstaged_paths():  # B-15
    assert "dirty.py" in _warn(" M dirty.py\n")
    assert "both.py" in _warn("MM both.py\n")


def test_staging_warning_reports_untracked_paths():  # B-15
    assert "scratch.py" in _warn("?? scratch.py\n")
    assert "notes.txt" in _warn("?? notes.txt\n")


def test_staging_warning_collects_every_path_not_just_the_first():
    msg = _warn(" M a.py\nMM b.py\n?? c.py\n?? d.txt\n")
    for name in ("a.py", "b.py", "c.py", "d.txt"):
        assert name in msg


def test_staging_warning_ignores_staged_only_lines():
    msg = _warn("M  staged.py\n M dirty.py\n")
    assert "dirty.py" in msg
    assert "staged.py" not in msg


def test_staging_warning_separates_paths_with_a_comma_and_space():
    """The tests above assert containment, which cannot see the separator: a
    mutated join still leaves every filename `in` the message. This asserts the
    rendered line, because the operator reads the line, not the substrings.

    Found 2026-08-02 by mutation — replacing `', '.join` with a marker string
    survived the whole suite.
    """
    assert "  untracked: c.py, d.txt" in _warn("?? c.py\n?? d.txt\n")
    assert "  unstaged: a.py, b.py" in _warn(" M b.py\n M a.py\n")


def test_staging_warning_promises_push_ci_only_where_it_exists():
    """The warning's last line tells the operator not to worry because push CI
    re-checks the commit. On a deployment that declined CI that sentence is
    false in the one place they are being reassured, so `has_push_ci` switches
    it. Both spellings are pinned: a mutant that blanks either string, or that
    ignores the flag, changes what the operator is promised.
    """
    dirty = " M x.py\n"
    with_ci = gate.staging_warning(dirty, has_push_ci=True)
    without_ci = gate.staging_warning(dirty, has_push_ci=False)
    assert with_ci is not None and without_ci is not None
    assert with_ci.endswith(
        "  a committed workflow references push CI — the same deterministic checks "
        "against the pushed tip."
    )
    assert without_ci.endswith(
        "  no push CI in this deployment — nothing re-checks the commit itself."
    )


def test_pyright_scope_reads_tool_mech():  # B-17
    assert gate.pyright_scope('[tool.mech]\npyright_scope = "whole"\n') == "whole"
    staged_cfg = '[tool.mech]\npyright_scope = "staged_plus_dependents"\n'
    assert gate.pyright_scope(staged_cfg) == "staged_plus_dependents"
    assert gate.pyright_scope("") == "whole"  # default


def test_run_gate_short_circuits_on_first_failure(monkeypatch):  # B-16, B-34
    calls = []

    def fake_step(name, ok=True):
        def _run():
            calls.append(name)
            return None if ok else f"{name} failed"

        return _run

    result = gate.run_steps(
        [
            ("ruff", fake_step("ruff")),
            ("pyright", fake_step("pyright", ok=False)),
            ("pytest", fake_step("pytest")),
        ],
        budget_s=600,
    )
    assert result == ("pyright", "pyright failed")
    assert calls == ["ruff", "pyright"]  # pytest never ran


def test_run_steps_watchdog(monkeypatch):  # B-31
    ticks = iter([0, 0, 700])

    def _run():
        return None

    result = gate.run_steps(
        [("ruff", _run), ("pyright", _run)], budget_s=600, clock=lambda: next(ticks)
    )
    assert result is not None and "600 s" in result[1] and result[0] == "pyright"


def test_run_steps_counts_time_elapsed_before_the_loop():
    """`run_steps` used to take its own `start = clock()`, so the aggregate
    budget check measured from the loop while the per-slot `remaining()` clamp
    measured from the gate — and pre-loop time (`git status`, the staging
    warning) escaped the watchdog entirely. With `start` supplied by the
    caller, budget already spent upstream times out the first step. Found by
    the 2026-08-02 most-defended panel (item 6)."""
    ran = []
    result = gate.run_steps(
        [("s", lambda: ran.append(1) and None)],
        budget_s=600,
        clock=lambda: 1000.0,
        start=0.0,
    )
    assert ran == []
    assert result is not None and "timeout" in result[1].lower()


def test_run_gate_watchdog_includes_pre_step_time(tmp_path):
    """The passthrough itself: run_gate hands run_steps the same start its
    `remaining()` closes over, so one clock governs both. First clock() call
    is the gate's start; everything after happens 1000 s later."""
    ticks = iter([0.0])
    result = gate.run_gate(
        _root(tmp_path),
        run=_quiet_run(tmp_path),
        clock=lambda: next(ticks, 1000.0),
    )
    assert result["outcome"] == verdict.FAIL
    assert "timeout" in result["findings"][0]["message"].lower()


# --- Cross-review additions (mandatory, test-first) --------------------------
# B-16/B-18/B-20/B-33/B-34 exercised at the run_gate level with scripted
# runners, per the brief's cross-review block.


def _completed(argv, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _root(tmp_path, pyproject_text='[tool.mech]\npyright_scope = "whole"\n'):
    if "[tool.mutmut]" not in pyproject_text:
        pyproject_text += '\n[tool.mutmut]\nsource_paths = ["mech"]\n'
    (tmp_path / "pyproject.toml").write_text(pyproject_text)
    return str(tmp_path)


def test_run_gate_runs_steps_despite_staging_divergence(tmp_path, capsys):
    calls = []

    def scripted_run(argv, **kwargs):
        calls.append(list(argv))
        return _completed(argv, 0, stdout=" M dirty.py\n" if not calls[:-1] else "")

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.PASS
    assert len(calls) > 1  # steps ran; the divergence did not abort the gate
    assert "dirty.py" in capsys.readouterr().err


def test_run_gate_tool_exception_is_error_and_stops(tmp_path):
    calls = []

    def scripted_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["git", "status"]:
            return _completed(argv, 0, stdout="")
        if "ruff" in argv and "format" in argv:
            raise OSError("ruff binary not found")
        raise AssertionError(f"step ran after ruff-format raised: {argv}")

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.ERROR
    assert result["findings"][0]["tool"] == "ruff-format"
    assert "ruff-format" in result["findings"][0]["message"]
    # only git status + the single raising ruff-format call happened
    assert len(calls) == 2


def test_run_gate_ruff_violation_exit_is_fail(tmp_path):
    """Sibling to the exception case: a scripted non-zero exit (a real tool
    violation, tool ran fine) classifies as `fail`, never `error`."""
    calls = []

    def scripted_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["git", "status"]:
            return _completed(argv, 0, stdout="")
        if "ruff" in argv and "format" in argv:
            return _completed(argv, 1, stdout="would reformat 1 file", stderr="")
        raise AssertionError(f"step ran after ruff-format failed: {argv}")

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.FAIL
    assert result["findings"][0]["tool"] == "ruff-format"
    assert len(calls) == 2


def _pass_until(*slots):
    """Build a scripted run() that passes git status + ruff/pyright, and lets
    the caller script what happens for the remaining named slot(s)."""

    def make(pytest_result):
        def scripted_run(argv, **kwargs):
            if argv[:2] == ["git", "status"]:
                return _completed(argv, 0, stdout="")
            if "pytest" in argv:
                return pytest_result(argv)
            return _completed(argv, 0)

        return scripted_run

    return make


def test_run_gate_pytest_exit5_is_error(tmp_path):
    scripted_run = _pass_until("pytest")(lambda argv: _completed(argv, 5))
    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.ERROR
    assert result["findings"][0]["tool"] == "pytest"
    assert "no tests" in result["findings"][0]["message"].lower()


def test_run_gate_pytest_failure_includes_seed_line(tmp_path):
    seed_line = "Using --randomly-seed=123456789"
    long_stdout = seed_line + "\n" + ("x" * 4000)
    scripted_run = _pass_until("pytest")(
        lambda argv: _completed(argv, 1, stdout=long_stdout, stderr="")
    )
    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.FAIL
    message = result["findings"][0]["message"]
    assert len(long_stdout) > 3000
    assert seed_line in message  # survives 3000-char tail truncation


def test_run_gate_pytest_failure_seedless_carries_unavailable(tmp_path):
    scripted_run = _pass_until("pytest")(
        lambda argv: _completed(argv, 1, stdout="no seed line here", stderr="")
    )
    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.FAIL
    assert "seed unavailable" in result["findings"][0]["message"]


def test_run_gate_non_pytest_failure_carries_seed_unavailable(tmp_path):
    def scripted_run(argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            return _completed(argv, 0, stdout="")
        if "ruff" in argv and "check" in argv:
            return _completed(argv, 1, stdout="E501 line too long", stderr="")
        return _completed(argv, 0)

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.FAIL
    assert result["findings"][0]["tool"] == "ruff-check"
    assert result["findings"][0]["message"].endswith("seed unavailable")


def test_step_timeout_clamped_to_remaining_budget():
    """REQ-ARCH-9 clamp: 350 s elapsed against a 600 s-table step budget
    (the mutation slot's table timeout) ⇒ the runner receives 250, not 600."""
    recorded = {}

    def fake_run(argv, cwd, capture_output, text, timeout, check, env):
        recorded["timeout"] = timeout
        return _completed(argv, 0)

    step = gate._tool_step(
        ["uv", "run", "mutmut", "run"],
        "/root",
        "mutation",
        timeout=600,
        run=fake_run,
        remaining=lambda: 250,
    )
    assert step() is None
    assert recorded["timeout"] == 250


def test_pyright_step_default_timeout_is_120(tmp_path):
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["timeout"] = kwargs.get("timeout")
        return _completed(argv, 0)

    root = _root(tmp_path)
    step = gate._pyright_step(root, ["uv", "run", "pyright"], run=fake_run)
    assert step() is None
    assert recorded["timeout"] == 120


def test_pyright_step_forwards_own_timeout_over_loose_remaining(tmp_path):
    # timeout=2 is tighter than remaining()=50: the clamp must pick the
    # forwarded `timeout`, not silently fall back to _tool_step's own
    # default (120), which a dropped-kwarg mutant would produce instead.
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["timeout"] = kwargs.get("timeout")
        return _completed(argv, 0)

    root = _root(tmp_path)
    step = gate._pyright_step(
        root, ["uv", "run", "pyright"], run=fake_run, timeout=2, remaining=lambda: 50
    )
    assert step() is None
    assert recorded["timeout"] == 2


def test_pyright_step_forwards_real_remaining_not_none(tmp_path):
    # timeout=5 is looser than remaining()=3: the clamp must pick the
    # forwarded `remaining`, not a mutant that silently forces it to None
    # (which would use the literal timeout=5 instead of the clamped 3).
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["timeout"] = kwargs.get("timeout")
        return _completed(argv, 0)

    root = _root(tmp_path)
    step = gate._pyright_step(
        root, ["uv", "run", "pyright"], run=fake_run, timeout=5, remaining=lambda: 3
    )
    assert step() is None
    assert recorded["timeout"] == 3


def test_pyright_step_empty_live_list_falls_back_whole_project(monkeypatch, tmp_path):
    """REQ-CG-3 fallback: staged deleted-only .py file (never present on
    disk, so `live` is empty) plus an empty importer closure ⇒ no files to
    narrow to, so argv stays exactly the base whole-project command."""
    monkeypatch.setattr(
        gate, "_staged_files", lambda root, timeout: ["mech/deleted.py"]
    )
    monkeypatch.setattr(
        imports_analyzer, "importers_closure", lambda root, seed_modules: set()
    )

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return _completed(argv, 0)

    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')
    step = gate._pyright_step(root, ["uv", "run", "pyright"], run=fake_run)
    assert step() is None
    assert recorded["argv"] == ["uv", "run", "pyright"]


def test_pyright_step_fails_closed_when_the_import_analyzer_raises(
    monkeypatch, tmp_path
):
    """REQ-CG-3: an unresolvable importer closure must abort the step, not fall
    through to an unscoped run. Falling through would type-check only the staged
    files, silently dropping the dependents the scope exists to catch, and
    report green."""
    (tmp_path / "mech").mkdir()
    (tmp_path / "mech" / "live.py").write_text("x = 1\n")
    monkeypatch.setattr(gate, "_staged_files", lambda root, timeout: ["mech/live.py"])

    def exploding(closure_root, seed_modules):
        raise imports_analyzer.AnalyzerError("unreadable module mech.live")

    monkeypatch.setattr(imports_analyzer, "importers_closure", exploding)

    ran = []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        return _completed(argv, 0)

    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')
    step = gate._pyright_step(root, list(tools.GATE[2][1]), run=fake_run)
    assert step() == "import analyzer failed: unreadable module mech.live"
    assert ran == []  # pyright must not run on a file set that never resolved


def test_pyright_step_scoped_argv_includes_staged_and_importers(monkeypatch, tmp_path):
    (tmp_path / "mech").mkdir()
    (tmp_path / "mech" / "live.py").write_text("x = 1\n")
    # mech/deleted.py is staged but absent on disk — feeds seeds only.
    staged = ["mech/live.py", "mech/deleted.py"]
    monkeypatch.setattr(gate, "_staged_files", lambda root, timeout: staged)

    seen_seeds = {}

    def fake_closure(closure_root, seed_modules):
        seen_seeds["root"] = closure_root
        seen_seeds["seeds"] = set(seed_modules)
        return {"mech.dependent"}

    monkeypatch.setattr(imports_analyzer, "importers_closure", fake_closure)

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return _completed(argv, 0)

    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')
    step = gate._pyright_step(root, list(tools.GATE[2][1]), run=fake_run)
    assert step() is None
    assert seen_seeds["root"] == root
    assert seen_seeds["seeds"] == {"mech.live", "mech.deleted"}
    argv = recorded["argv"]
    assert "mech/live.py" in argv
    assert "mech/dependent.py" in argv
    assert "mech/deleted.py" not in argv  # deleted: seeds only, never argv


def test_commit_gate_main_exit1_stderr(monkeypatch, tmp_path, capsys):
    from mech import commit_gate

    monkeypatch.setattr(paths, "repo_root", lambda cwd: str(tmp_path))
    finding = verdict.finding("ruff-format", "would reformat x.py")

    monkeypatch.setattr(
        commit_gate.gate, "run_gate", lambda root: verdict.failed([finding])
    )
    exit_code = commit_gate.main()
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "would reformat x.py" in captured.err


def test_commit_gate_main_no_repo_root(monkeypatch, capsys):
    from mech import commit_gate

    monkeypatch.setattr(paths, "repo_root", lambda cwd: None)
    exit_code = commit_gate.main()
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == "commit gate: no git repo root — refusing to certify\n"


def test_commit_gate_main_exception_prints_error_and_escape_hatch(
    monkeypatch, tmp_path, capsys
):
    from mech import commit_gate

    monkeypatch.setattr(paths, "repo_root", lambda cwd: str(tmp_path))

    def boom(root):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(commit_gate.gate, "run_gate", boom)
    exit_code = commit_gate.main()
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "commit gate errored: RuntimeError: kaboom\n"
        "Human escape hatch (mech bug blocking its own fix): "
        "git commit --no-verify\n"
    )


def test_commit_gate_main_pass_returns_0_silently(monkeypatch, tmp_path, capsys):
    from mech import commit_gate

    monkeypatch.setattr(paths, "repo_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(commit_gate.gate, "run_gate", lambda root: verdict.passed())
    exit_code = commit_gate.main()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_commit_gate_main_threads_real_cwd_and_root(monkeypatch, tmp_path):
    from mech import commit_gate

    received = {}

    def fake_repo_root(cwd):
        received["cwd"] = cwd
        return str(tmp_path)

    def fake_run_gate(root):
        received["root"] = root
        return verdict.passed()

    monkeypatch.setattr(paths, "repo_root", fake_repo_root)
    monkeypatch.setattr(commit_gate.gate, "run_gate", fake_run_gate)
    commit_gate.main()
    assert received["cwd"] == os.getcwd()
    assert received["root"] == str(tmp_path)


def test_commit_gate_main_uses_nested_project_cwd(monkeypatch):
    from mech import commit_gate

    received = {}

    def fake_run_gate(root):
        received["root"] = root
        return verdict.passed()

    def fake_project_root(cwd, repository_root):
        received["resolution"] = (cwd, repository_root)
        return "/repo/service"

    monkeypatch.setattr(commit_gate.os, "getcwd", lambda: "/repo/service")
    monkeypatch.setattr(paths, "repo_root", lambda _cwd: "/repo")
    monkeypatch.setattr(paths, "project_root", fake_project_root)
    monkeypatch.setattr(commit_gate.gate, "run_gate", fake_run_gate)

    assert commit_gate.main() == 0
    assert received == {
        "resolution": ("/repo/service", "/repo"),
        "root": "/repo/service",
    }


# --- Mutation-hardening additions (dogfood run, REQ-B-2) --------------------
# The task-12 dogfood mutation-testing pass surfaced real survivors in
# mech/gate.py and mech/commit_gate.py: assertions below were too loose
# (substring/None checks) to distinguish exact-string, sign, boundary, and
# argument-threading mutants from the real behavior. Tightened/added here
# rather than classified in MUTANTS.md, since each is cheap and behavior-real.


def test_staging_warning_exact_message():
    assert gate.staging_warning(" M dirty.py\nMM both.py\n?? scratch.py\n") == (
        "staged subset — the gate verified the WORKING TREE, not this commit.\n"
        "  unstaged: both.py, dirty.py\n"
        "  untracked: scratch.py\n"
        "  a committed workflow references push CI — the same deterministic checks "
        "against the pushed tip."
    )


def test_staging_warning_two_char_line_boundary():
    # len(line) >= 2, not > 2 or >= 3: a bare 2-char status line still counts.
    assert gate.staging_warning("MM\n") is not None


def test_staging_warning_drops_the_push_ci_claim_when_there_is_no_push_ci():
    # The sentence is the reassurance attached to "the gate verified the tree,
    # not this commit". A deployment that declined CI has no such backstop, and
    # this is the one place the user is being told not to worry.
    msg = gate.staging_warning(" M dirty.py\n", has_push_ci=False)
    assert msg is not None
    assert "same deterministic checks" not in msg


def test_staging_warning_names_the_missing_backstop_rather_than_going_quiet():
    # Silence would read as "nothing to say here"; the gap is worth naming.
    msg = gate.staging_warning(" M dirty.py\n", has_push_ci=False)
    assert msg is not None
    assert "no push CI" in msg
    assert "nothing re-checks the commit itself" in msg


# Same guard as test_ci.py, and for the same reason: `mech/tests/` travels into
# every deployment, so a test that asserts Pawl's own environment fails in any
# repo that declined CI. This one exists to pin the predicate against the real
# push.yml, which a CI-less deployment does not have and cannot pin.
@pytest.mark.skipif(
    not os.path.isdir(
        os.path.join(paths.repo_root(os.getcwd()) or ".", ".github", "workflows")
    ),
    reason="no .github/workflows/ — this deployment declined CI",
)
def test_push_ci_present_is_true_for_pawl_itself():
    # Pins the predicate to the real workflow: if push.yml ever stops invoking
    # mech.ci_push in a form this matcher recognises, this fails here rather
    # than silently downgrading every downstream staging warning.
    assert gate.push_ci_present(paths.repo_root(os.getcwd()) or ".") is True


def test_push_ci_present_finds_repo_workflow_above_nested_project(tmp_path):
    subprocess.run(  # noqa: S603  # fixed fixture command
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved git; fixture only
        cwd=tmp_path,
        check=True,
    )
    project = tmp_path / "service"
    project.mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "push.yml").write_text(
        "steps:\n  - run: uv run python -m mech.ci_push\n", encoding="utf-8"
    )

    assert gate.push_ci_present(str(project)) is True


def test_push_ci_present_ignores_commented_out_invocations(tmp_path):
    """A workflow whose only `-m mech.ci_push` sits in a YAML comment is not
    push CI — the staging warning would reassure the operator with a run that
    will never happen. Found by the 2026-08-02 most-defended panel (item 4)."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "x.yml").write_text(
        # third line: the invocation sits between two '#'s — an rsplit-shaped
        # comment strip would keep it and false-positive
        "# uv run python -m mech.ci_push\n"
        "run: echo hi  # -m mech.ci_push\n"
        "# uv run python -m mech.ci_push  # see push.yml\n",
        encoding="utf-8",
    )
    assert gate.push_ci_present(str(tmp_path)) is False


def test_push_ci_present_is_false_when_there_is_no_workflows_dir(tmp_path):
    assert gate.push_ci_present(str(tmp_path)) is False


def test_push_ci_present_is_false_when_no_workflow_invokes_ci_push(tmp_path):
    # A repo can have CI without having *pawl's* push CI; the claim is about
    # the four checks, not about GitHub Actions existing.
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "lint.yml").write_text("run: echo hello\n", encoding="utf-8")
    assert gate.push_ci_present(str(tmp_path)) is False


def test_push_ci_present_ignores_non_yaml_files(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "notes.txt").write_text("python -m mech.ci_push\n", encoding="utf-8")
    assert gate.push_ci_present(str(tmp_path)) is False


def test_push_ci_present_skips_non_yaml_before_valid_workflow(tmp_path):
    """Ignoring one unrelated entry must not stop workflow discovery."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / ".artifact").write_text("not a workflow\n", encoding="utf-8")
    (workflows / "push.yml").write_text(
        "run: uv run python -m mech.ci_push\n", encoding="utf-8"
    )
    assert gate.push_ci_present(str(tmp_path)) is True


def test_format_pytest_failure_exact_tail_boundaries():
    stdout = "A" * 3005
    stderr = "B" * 1005
    result = gate.format_pytest_failure(stdout, stderr)
    assert result == "A" * 3000 + "B" * 1000 + "\nseed unavailable"


def test_format_pytest_failure_seedless_exact():
    assert gate.format_pytest_failure("no seed here", "") == (
        "no seed here\nseed unavailable"
    )


def test_run_steps_budget_boundary_not_exceeded():
    def _run():
        return None

    ticks = iter([0, 600])  # elapsed exactly == budget_s: not yet exceeded
    result = gate.run_steps([("ruff", _run)], budget_s=600, clock=lambda: next(ticks))
    assert result is None


def test_run_steps_watchdog_exact_message():
    def _run():
        return None

    ticks = iter([0, 700])
    result = gate.run_steps(
        [("pyright", _run)], budget_s=600, clock=lambda: next(ticks)
    )
    assert result == (
        "pyright",
        "Commit gate timeout: step 'pyright' exceeded 600 s total runtime",
    )


def test_gate_error_message_and_step():
    exc = gate.GateError("ruff-format", "boom")
    assert exc.step == "ruff-format"
    assert exc.message == "boom"
    assert str(exc) == "boom"


def test_scrubbed_env_removes_scrub_list_keeps_others(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    env = tools.scrubbed_env()
    assert "PYTEST_ADDOPTS" not in env
    assert env.get("SOME_OTHER_VAR") == "keep-me"


def test_scrubbed_env_removes_git_repo_redirection(monkeypatch):
    """The gate only ever spawns tools from inside a git hook, which is the one
    moment git exports these. Each one aims a git command somewhere other than
    where a plain shell would aim it, so an inherited value makes a test that
    builds its own repository operate on the in-flight commit instead."""
    for name in (
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
    ):
        monkeypatch.setenv(name, "/outer/repo/value")
        assert name not in tools.scrubbed_env(), name


def test_scrubbed_env_removes_uv_redirection(monkeypatch):
    """Every row in tools.GATE and tools.CI begins with `uv run`, so a variable
    that redirects uv redirects the whole gate. Measured 2026-08-02:
    `UV_WORKING_DIR=<other project>` makes the gate exit 0 with zero findings
    on a tree carrying four real ruff violations, because each step ran in the
    other directory; `UV_ENV_FILE` re-injects PYTEST_ADDOPTS *after* this
    function removed it. Same class as the GIT_INDEX_FILE incident that this
    list was widened for on 2026-07-27, one tool over."""
    for name in ("UV_WORKING_DIR", "UV_PROJECT", "UV_ENV_FILE", "UV_PYTHON"):
        monkeypatch.setenv(name, "/somewhere/else")
        assert name not in tools.scrubbed_env(), name


def test_every_gate_and_ci_row_that_runs_uv_is_covered_by_the_uv_scrub():
    """The premise of the test above, asserted rather than assumed: if a row
    stops going through `uv`, or a new tool is added that does, this pairing
    should be re-examined rather than silently over- or under-scrubbing."""
    uv_rows = [row for row in tools.GATE if row[1][0] == "uv"]
    assert len(uv_rows) == len(tools.GATE)


def test_gate_has_a_lightweight_pawl_compatibility_record_check():
    row = next(row for row in tools.GATE if row[0] == "pawl-compatibility")
    assert row == (
        "pawl-compatibility",
        ("uv", "run", "python", "-m", "mech.selftest", "--check"),
        60,
        "repo",
    )
    assert any(row[1][0] == "uv" for row in tools.CI)


def test_required_test_commands_use_the_fixed_gate_seed():
    """Commit and push verdicts for one revision must use one test order.

    The report-only Stop hook deliberately remains outside this assertion so it
    can explore other orders without making an authoritative verdict vary.
    """
    gate_pytest = next(row for row in tools.GATE if row[0] == "pytest")
    coverage_pytest = next(row for row in tools.CI if row[0] == "ci-coverage-run")

    assert tools.REQUIRED_PYTEST_SEED_ARG == "--randomly-seed=20260901"
    assert tools.REQUIRED_PYTEST_SEED_ARG in gate_pytest[1]
    assert tools.REQUIRED_PYTEST_SEED_ARG in coverage_pytest[1]


def test_scrubbed_env_keeps_git_vars_that_redirect_nothing(monkeypatch):
    """GIT_EXEC_PATH tells git where its own helpers live; scrubbing it would
    degrade the child's git for no safety gain."""
    monkeypatch.setenv("GIT_EXEC_PATH", "/usr/libexec/git-core")
    assert tools.scrubbed_env().get("GIT_EXEC_PATH") == "/usr/libexec/git-core"


def test_spawned_tool_never_receives_in_flight_commit_index(monkeypatch):
    """Boundary contract: whatever the gate hands a subprocess is what that
    tool's own children inherit, so the scrub has to hold at the seam."""
    monkeypatch.setenv("GIT_INDEX_FILE", "/repo/.git/next-index-4242.lock")
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return _completed(argv, 0)

    gate._tool_step(["pytest"], "/root", "pytest", run=fake_run)()
    assert "GIT_INDEX_FILE" not in recorded["env"]


def test_tool_step_passes_expected_subprocess_kwargs(monkeypatch):
    monkeypatch.setenv("KEEP_ME", "1")
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return _completed(argv, 0)

    step = gate._tool_step(["echo", "hi"], "/root", "ruff-format", run=fake_run)
    assert step() is None
    assert recorded["cwd"] == "/root"
    assert recorded["capture_output"] is True
    assert recorded["text"] is True
    assert recorded["check"] is False
    assert recorded["timeout"] == 120
    assert "KEEP_ME" in recorded["env"]


def test_staged_files_returns_git_quoted_pathnames_unquoted(tmp_path):
    """On plain `--name-only` output git C-quotes any path containing a
    special character, so `weird\\tname.py` came back as `'"weird\\tname.py"'`
    and failed `endswith(".py")` — under `staged_plus_dependents` that file
    was silently dropped from pyright's scoped list while its siblings kept
    the run out of the whole-project fallback. `-z` output is NUL-separated
    and never quoted. Dormant in pawl (scope `"whole"`); live for any
    deployment that opts into scoping. Found by the 2026-08-02 most-defended
    panel (item 8)."""
    subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", "init", "-q", str(tmp_path)],  # noqa: S607  # PATH-resolved `git` is intentional
        check=True,
    )
    (tmp_path / "weird\tname.py").write_text("x = 1\n")
    subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", "add", "-A"],  # noqa: S607  # PATH-resolved `git` is intentional
        cwd=str(tmp_path),
        check=True,
    )
    assert gate._staged_files(str(tmp_path)) == ["weird\tname.py"]


def test_staged_files_preserves_carriage_returns_in_pathnames(tmp_path):
    """NUL framing prevents Git quoting, but text mode still performs
    universal-newline translation and changes a literal carriage return."""
    subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", "init", "-q", str(tmp_path)],  # noqa: S607  # PATH-resolved git is intentional
        check=True,
    )
    name = "cr\rname.py"
    (tmp_path / name).write_text("x = 1\n")
    subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", "add", "-A"],  # noqa: S607  # PATH-resolved git is intentional
        cwd=str(tmp_path),
        check=True,
    )

    assert gate._staged_files(str(tmp_path)) == [name]


def test_staged_files_are_relative_to_nested_project_and_exclude_siblings(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    project = tmp_path / "service"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("y = 2\n", encoding="utf-8")
    _git("add", "service/app.py", "outside.py", cwd=tmp_path)

    assert gate._staged_files(str(project)) == ["app.py"]


def test_staged_files_nonzero_git_exit_fails_closed(tmp_path):
    with pytest.raises(OSError, match="git staged-file discovery exited"):
        gate._staged_files(str(tmp_path))


def test_staged_files_subprocess_contract_and_error_tail_are_exact(
    monkeypatch, tmp_path
):
    recorded = {}

    def failed_git(argv, **kwargs):
        recorded["argv"] = argv
        recorded.update(kwargs)
        return subprocess.CompletedProcess(
            argv, 128, stdout=b"", stderr=b"A" * 600 + b"B" * 500
        )

    monkeypatch.setattr(gate.subprocess, "run", failed_git)
    with pytest.raises(OSError) as exc:
        gate._staged_files(str(tmp_path))

    assert recorded == {
        "argv": ["git", "diff", "--cached", "--name-only", "-z", "--relative"],
        "cwd": str(tmp_path),
        "capture_output": True,
        "text": False,
        "timeout": 120,
        "check": False,
    }
    assert str(exc.value) == "git staged-file discovery exited 128: " + "B" * 500


def test_pyright_step_staged_discovery_exception_is_infrastructure_error(
    monkeypatch, tmp_path
):
    def timed_out(root, timeout):
        raise subprocess.TimeoutExpired(["git", "diff"], timeout)

    monkeypatch.setattr(gate, "_staged_files", timed_out)
    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')

    with pytest.raises(gate.GateError) as exc:
        gate._pyright_step(root, ["uv", "run", "pyright"])()
    assert exc.value.step == "pyright"
    assert exc.value.message == (
        "pyright staged-file discovery could not run: TimeoutExpired: "
        "Command '['git', 'diff']' timed out after 120 seconds"
    )


def test_pyright_staged_discovery_timeout_clamps_to_remaining_budget(
    monkeypatch, tmp_path
):
    recorded = {}

    def no_staged_files(root, timeout):
        recorded.update(root=root, timeout=timeout)
        return []

    monkeypatch.setattr(gate, "_staged_files", no_staged_files)
    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')
    step = gate._pyright_step(
        root,
        ["uv", "run", "pyright"],
        run=lambda argv, **kwargs: _completed(argv, 0),
        timeout=120,
        remaining=lambda: 0,
    )

    assert step() is None
    assert recorded == {"root": root, "timeout": 1}


def test_tool_step_default_no_tests_is_error_false():
    def fake_run(argv, **kwargs):
        return _completed(argv, 5, stdout="", stderr="")

    step = gate._tool_step(["x"], "/root", "ruff-format", run=fake_run)
    result = step()  # no_tests_is_error defaults False: exit 5 is a plain failure
    assert result is not None and result.endswith("seed unavailable")


def test_tool_step_negative_returncode_is_an_error_not_a_fail():
    """A tool killed by a signal produced no verdict; calling it `fail` tells
    the user their code is bad when the check never completed. REQ-ARCH-6
    reserves `error` for exactly this. Found by the 2026-08-02 most-defended
    panel (item 5)."""

    def fake_run(argv, **kwargs):
        return _completed(argv, -9)

    step = gate._tool_step(["x"], "/root", "ruff-check", run=fake_run)
    with pytest.raises(gate.GateError) as exc:
        step()
    assert "signal 9" in exc.value.message
    assert exc.value.step == "ruff-check"  # the errored verdict names the tool


@pytest.mark.parametrize(
    ("code", "meaning"),
    [(2, "interrupted"), (3, "internal error"), (4, "usage error")],
)
def test_tool_step_pytest_infrastructure_exits_are_errors(code, meaning):
    """pytest exits 2/3/4 are statements about the run, not about the code
    (same panel item as the signal case). The message keeps the output tail —
    an INTERNAL_ERROR without its traceback is undiagnosable."""

    def fake_run(argv, **kwargs):
        return _completed(argv, code, stdout="the-diagnostic", stderr="")

    step = gate._tool_step(
        ["uv", "run", "pytest"], "/root", "pytest", no_tests_is_error=True, run=fake_run
    )
    with pytest.raises(gate.GateError) as exc:
        step()
    assert meaning in exc.value.message
    assert "the-diagnostic" in exc.value.message
    assert exc.value.step == "pytest"


def test_tool_step_infra_exit_tail_exact_boundaries():
    """Same window contract as `format_pytest_failure`: last 3000 of stdout,
    last 1000 of stderr — pinned exactly so a slice-bound mutant cannot
    silently trim or widen the diagnostic."""

    def fake_run(argv, **kwargs):
        return _completed(argv, 3, stdout="A" * 3005, stderr="B" * 1005)

    step = gate._tool_step(
        ["uv", "run", "pytest"], "/root", "pytest", no_tests_is_error=True, run=fake_run
    )
    with pytest.raises(gate.GateError) as exc:
        step()
    assert exc.value.message.endswith("\n" + "A" * 3000 + "B" * 1000)


def test_tool_step_unknown_positive_exit_stays_a_failure():
    """Infrastructure mappings are per-tool contracts, not a general rule."""

    def fake_run(argv, **kwargs):
        return _completed(argv, 2, stdout="", stderr="")

    step = gate._tool_step(["x"], "/root", "pawl-compatibility", run=fake_run)
    assert step() is not None  # a string: fail, not GateError


@pytest.mark.parametrize("name", ["ruff-format", "ruff-check"])
def test_tool_step_ruff_internal_exit_is_an_error(name):
    def fake_run(argv, **kwargs):
        return _completed(argv, 2, stdout="ruff diagnostic")

    with pytest.raises(gate.GateError) as exc:
        gate._tool_step(["uv", "run", "ruff"], "/root", name, run=fake_run)()
    assert "tool or configuration error" in exc.value.message
    assert "ruff diagnostic" in exc.value.message


@pytest.mark.parametrize(
    ("code", "meaning"),
    [(2, "fatal error"), (3, "configuration error"), (4, "command-line error")],
)
def test_tool_step_pyright_infrastructure_exits_are_errors(code, meaning):
    def fake_run(argv, **kwargs):
        return _completed(argv, code, stderr="pyright diagnostic")

    with pytest.raises(gate.GateError) as exc:
        gate._tool_step(["uv", "run", "pyright"], "/root", "pyright", run=fake_run)()
    assert meaning in exc.value.message
    assert "pyright diagnostic" in exc.value.message


def test_tool_step_non_pytest_infra_exit_tail_and_tool_are_exact():
    def fake_run(argv, **kwargs):
        return _completed(argv, 2, stdout="A" * 3005, stderr="B" * 1005)

    with pytest.raises(gate.GateError) as exc:
        gate._tool_step(["uv", "run", "pyright"], "/root", "pyright", run=fake_run)()
    assert exc.value.step == "pyright"
    assert exc.value.message.endswith("\n" + "A" * 3000 + "B" * 1000)


def test_tool_step_timeout_floor_is_one():
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["timeout"] = kwargs["timeout"]
        return _completed(argv, 0)

    step = gate._tool_step(
        ["x"], "/root", "n", timeout=120, run=fake_run, remaining=lambda: 0
    )
    step()
    assert recorded["timeout"] == 1


def test_tool_step_exception_message_names_real_type():
    def fake_run(argv, **kwargs):
        raise ValueError("bad")

    step = gate._tool_step(["x"], "/root", "ruff-format", run=fake_run)
    try:
        step()
        raise AssertionError("expected GateError")
    except gate.GateError as exc:
        assert "ValueError" in exc.message
        assert "bad" in exc.message


def test_tool_step_non_pytest_failure_tail_exact_boundaries():
    def fake_run(argv, **kwargs):
        return _completed(argv, 1, stdout="A" * 3005, stderr="B" * 1005)

    step = gate._tool_step(["x"], "/root", "ruff-check", run=fake_run)
    result = step()
    assert result == (
        "ruff-check failed:\n" + "A" * 3000 + "B" * 1000 + "\nseed unavailable"
    )


def test_build_steps_threads_args_to_each_factory(monkeypatch):
    pyright_call = {}
    tool_calls = []

    def fake_pyright_step(root, argv, run=None, timeout=None, remaining=None):
        pyright_call["root"] = root
        pyright_call["argv"] = argv
        pyright_call["run"] = run
        pyright_call["timeout"] = timeout
        pyright_call["remaining"] = remaining
        return lambda: None

    def fake_tool_step(
        argv,
        root,
        name,
        timeout=120,
        no_tests_is_error=False,
        run=None,
        remaining=None,
    ):
        tool_calls.append(
            {
                "argv": argv,
                "root": root,
                "name": name,
                "timeout": timeout,
                "no_tests_is_error": no_tests_is_error,
                "run": run,
                "remaining": remaining,
            }
        )
        return lambda: None

    monkeypatch.setattr(gate, "_pyright_step", fake_pyright_step)
    monkeypatch.setattr(gate, "_tool_step", fake_tool_step)

    sentinel_run = object()
    sentinel_remaining = object()
    steps = gate._build_steps(
        "/root", table=tools.GATE, run=sentinel_run, remaining=sentinel_remaining
    )
    assert [name for name, _ in steps] == [row[0] for row in tools.GATE]

    assert pyright_call == {
        "root": "/root",
        "argv": list(tools.GATE[2][1]),
        "run": sentinel_run,
        "timeout": tools.GATE[2][2],
        "remaining": sentinel_remaining,
    }
    by_name = {c["name"]: c for c in tool_calls}
    assert by_name["ruff-format"]["root"] == "/root"
    assert by_name["ruff-format"]["run"] is sentinel_run
    assert by_name["ruff-format"]["remaining"] is sentinel_remaining
    assert by_name["ruff-format"]["timeout"] == 120
    assert by_name["ruff-format"]["no_tests_is_error"] is False
    assert by_name["pytest"]["timeout"] == 300
    assert by_name["pytest"]["no_tests_is_error"] is True


def _spy_open_encoding(monkeypatch, suffix):
    """Patch builtins.open to record the `encoding` kwarg used to open any
    path ending in `suffix`, while still performing the real read."""
    real_open = builtins.open
    captured = {}

    def spy(path, *args, **kwargs):
        if str(path).endswith(suffix):
            captured["encoding"] = kwargs.get("encoding")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    return captured


def test_pyright_step_reads_pyproject_with_utf8_encoding(monkeypatch, tmp_path):
    root = _root(tmp_path)
    captured = _spy_open_encoding(monkeypatch, "pyproject.toml")
    gate._pyright_step(
        root, ["uv", "run", "pyright"], run=lambda argv, **kw: _completed(argv, 0)
    )()
    assert captured["encoding"] == "utf-8"


def test_pyright_step_wires_root_name_and_extends_argv(monkeypatch, tmp_path):
    (tmp_path / "mech").mkdir()
    (tmp_path / "mech" / "live.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("x = 1\n")
    staged = ["mech/live.py", "tests/test_x.py"]
    received_root = {}

    def fake_staged(root, timeout):
        received_root["root"] = root
        received_root["timeout"] = timeout
        return staged

    monkeypatch.setattr(gate, "_staged_files", fake_staged)

    seen_seeds = {}

    def fake_closure(root, seed_modules):
        seen_seeds["seeds"] = set(seed_modules)
        return set()

    monkeypatch.setattr(imports_analyzer, "importers_closure", fake_closure)

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["cwd"] = kwargs.get("cwd")
        return _completed(argv, 1, stdout="err", stderr="")

    root = _root(tmp_path, '[tool.mech]\npyright_scope = "staged_plus_dependents"\n')
    step = gate._pyright_step(root, ["uv", "run", "pyright"], run=fake_run)
    result = step()
    assert received_root == {"root": root, "timeout": 120}
    # module_for("mech/live.py") is truthy: `or` never evaluates _dotted for
    # it; module_for("tests/test_x.py") is None: `_dotted` fallback kicks in.
    assert seen_seeds["seeds"] == {"mech.live", "tests.test_x"}
    assert recorded["argv"][:3] == ["uv", "run", "pyright"]
    assert recorded["cwd"] == root
    assert result is not None and result.startswith("pyright failed:")


def test_run_gate_says_so_when_the_staging_check_itself_fails(tmp_path, capsys):
    """A failed `git status` returns empty stdout, which is byte-identical to a
    clean tree — so the gate used to certify silently in the one case where it
    could not know what the commit contained. Since REQ-CG-2 became advisory in
    2026-07-25 this line is the only thing announcing that the gate verified a
    superset of the commit, so it must never be quiet by accident."""

    def scripted_run(argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            return _completed(argv, 128, stdout="", stderr="fatal: bad object HEAD")
        return _completed(argv, 0)

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    err = capsys.readouterr().err
    assert "staging" in err
    assert "128" in err  # names the exit code
    assert "bad object HEAD" in err  # and git's own reason
    # advisory, not fail-closed: the four real steps still ran and passed
    assert result["outcome"] == verdict.PASS


def test_run_gate_clean_tree_prints_nothing(tmp_path, capsys):
    """Guards the fix above from over-firing: a genuinely clean tree must stay
    silent, or the warning becomes noise everyone learns to ignore."""

    def scripted_run(argv, **kwargs):
        return _completed(argv, 0, stdout="")

    gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert capsys.readouterr().err == ""


def test_run_gate_git_status_call_kwargs_exact(tmp_path):
    recorded = {}

    def scripted_run(argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            recorded["argv"] = argv
            recorded.update(kwargs)
        return _completed(argv, 0, stdout="")

    root = _root(tmp_path)
    gate.run_gate(root, run=scripted_run, clock=lambda: 0)
    # --untracked-files=all is load-bearing, not cosmetic: it overrides a user's
    # status.showUntrackedFiles=no, under which every `??` line disappears and
    # the warning goes silent on exactly the "forgot to git add the new module"
    # case it exists for. It also lists files rather than collapsing a directory.
    assert recorded["argv"] == [
        "git",
        "status",
        "--porcelain",
        "--untracked-files=all",
    ]
    assert recorded["cwd"] == root
    assert recorded["capture_output"] is True
    assert recorded["text"] is True
    assert recorded["check"] is False
    assert recorded["timeout"] == gate.BUDGET_S


def test_run_gate_git_status_timeout_is_an_error(tmp_path):
    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = gate.run_gate(_root(tmp_path), run=timed_out, clock=lambda: 0)

    assert result["outcome"] == verdict.ERROR
    assert result["findings"][0]["tool"] == "staging"
    assert "TimeoutExpired" in result["findings"][0]["message"]
    assert "timed out" in result["findings"][0]["message"]


def test_run_gate_staging_warning_goes_to_stderr_not_the_verdict(tmp_path, capsys):
    seen = []

    def scripted_run(argv, **kwargs):
        seen.append(list(argv))
        return _completed(argv, 0, stdout="?? scratch.py\n" if not seen[:-1] else "")

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["outcome"] == verdict.PASS
    assert result["findings"] == []  # a warning is not a finding
    err = capsys.readouterr().err
    assert err.startswith("staging: staged subset")
    assert "scratch.py" in err


def test_run_gate_remaining_budget_clamps_repo_step_timeout(monkeypatch, tmp_path):
    recorded = {}
    call_count = {"n": 0}

    def sticky_clock():
        # first call is run_gate's own `start`; every call after is pinned to
        # a fixed later instant, so elapsed is stable regardless of exactly
        # how many internal clock() reads happen along the way.
        call_count["n"] += 1
        return 100 if call_count["n"] == 1 else 650

    # Skip the mutation step (its own subprocess calls aren't scripted here).
    monkeypatch.setattr(gate, "_staged_files", lambda root, timeout: [])

    def scripted_run(argv, **kwargs):
        if "ruff" in argv and "format" in argv:
            recorded["timeout"] = kwargs.get("timeout")
        return _completed(argv, 0, stdout="")

    gate.run_gate(_root(tmp_path), run=scripted_run, clock=sticky_clock)
    # elapsed = 650 - 100 = 550; remaining = 600 - 550 = 50; ruff-format's
    # table timeout is 120, so the clamp to 50 is only visible if the sign
    # and the (clock() - start) order are both correct.
    assert recorded["timeout"] == 50


def test_run_gate_wires_build_steps_and_run_steps(monkeypatch, tmp_path):
    build_calls = {}

    def fake_build_steps(root, table=tools.GATE, run=None, remaining=None):
        build_calls["root"] = root
        build_calls["run"] = run
        build_calls["remaining"] = remaining
        return []

    run_steps_calls = {}

    def fake_run_steps(steps, budget_s=600, clock=None, start=None):
        run_steps_calls["clock"] = clock
        run_steps_calls["steps"] = steps
        run_steps_calls["start"] = start
        return None

    monkeypatch.setattr(gate, "_build_steps", fake_build_steps)
    monkeypatch.setattr(gate, "run_steps", fake_run_steps)

    def scripted_run(argv, **kwargs):
        return _completed(argv, 0, stdout="")

    sentinel_clock = lambda: 0  # noqa: E731  # identity-compared sentinel

    root = _root(tmp_path)
    result = gate.run_gate(root, run=scripted_run, clock=sentinel_clock)
    assert build_calls["root"] == root
    assert build_calls["run"] is scripted_run
    assert build_calls["remaining"] is not None
    assert run_steps_calls["clock"] is sentinel_clock
    assert run_steps_calls["start"] == 0  # run_gate's own start, not a fresh one
    assert result["outcome"] == verdict.PASS


def test_run_gate_fail_summary_exact(tmp_path):
    def scripted_run(argv, **kwargs):
        if argv[:2] == ["git", "status"]:
            return _completed(argv, 0, stdout="")
        if "ruff" in argv and "format" in argv:
            return _completed(argv, 1, stdout="would reformat 1 file", stderr="")
        raise AssertionError(f"step ran after ruff-format failed: {argv}")

    result = gate.run_gate(_root(tmp_path), run=scripted_run, clock=lambda: 0)
    assert result["summary"] == "ruff-format blocked the commit"


def _quiet_run(tmp_path, status_out="", status_rc=0, status_err=""):
    """A `run` seam where only `git status` is scripted and every gate step
    passes, so run_gate's staging report is the only thing under test."""

    def scripted(argv, **kwargs):  # noqa: ARG001  # stub must accept the kwargs it discards
        if argv[:2] == ["git", "status"]:
            return _completed(argv, status_rc, stdout=status_out, stderr=status_err)
        return _completed(argv, 0)

    return scripted


def _write_push_ci(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "push.yml").write_text(
        "on:\n  push:\njobs:\n  gate:\n    steps:\n"
        "      - run: uv run python -m mech.ci_push\n",
        encoding="utf-8",
    )


def test_push_ci_present_accepts_a_yaml_extension_too(tmp_path):
    """GitHub Actions accepts both spellings and `push_ci_present` matches both,
    but every fixture here and every workflow in this repo uses `.yml` — so the
    `.yaml` half of that tuple was never executed. A deployment that named its
    workflow `push.yaml` would have been told it has no push CI, and the staging
    warning would have understated its own guarantee.

    Found 2026-08-02 by mutation: blanking and upper-casing `".yaml"` in the
    extension tuple both survived.
    """
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "push.yaml").write_text(
        "jobs:\n  gate:\n    steps:\n      - run: uv run python -m mech.ci_push\n",
        encoding="utf-8",
    )
    assert gate.push_ci_present(str(tmp_path)) is True


def test_run_gate_says_push_ci_rechecks_when_the_deployment_has_it(tmp_path, capsys):
    """run_gate must pass `push_ci_present(root)` through, not a constant. The
    reassurance that a committed workflow references push CI is only true where
    one does.

    Built as a tmp deployment rather than read off this repo: `mech/tests/`
    travels into every deployment, so asserting Pawl's own `.github/` would fail in a
    repo that legitimately declined CI.
    """
    _write_push_ci(tmp_path)
    gate.run_gate(
        _root(tmp_path), run=_quiet_run(tmp_path, " M x.py\n"), clock=lambda: 0
    )
    assert "a committed workflow references push CI" in capsys.readouterr().err


def test_run_gate_admits_nothing_rechecks_when_the_deployment_declined_ci(
    tmp_path, capsys
):
    """The other direction, and the one that matters: with no push CI the
    guarantee has not moved, it is gone, and the warning must say so instead of
    promising a run that will never happen.

    Found 2026-08-02 by mutation — dropping the `has_push_ci=` argument
    entirely (so the default True applies) survived, because no test drove
    run_gate on a root without push CI.
    """
    gate.run_gate(
        _root(tmp_path), run=_quiet_run(tmp_path, " M x.py\n"), clock=lambda: 0
    )
    err = capsys.readouterr().err
    assert "no push CI in this deployment" in err
    assert "a committed workflow references push CI" not in err


def test_run_gate_failed_status_stderr_tail_is_exactly_last_500(tmp_path, capsys):
    """A failed `git status` is reported advisory, with its stderr tail bounded
    so a runaway message cannot bury the gate's own output. The boundary is the
    behaviour; an off-by-one in it survived the suite until this test.
    """
    long_err = "A" * 600 + "B" * 500
    gate.run_gate(
        _root(tmp_path),
        run=_quiet_run(tmp_path, status_rc=128, status_err=long_err),
        clock=lambda: 0,
    )
    err = capsys.readouterr().err
    assert "`git status` exited 128: " + "B" * 500 in err
    assert "A" * 601 not in err


def test_dotted_helper():
    assert gate._dotted("mech/foo/bar.py") == "mech.foo.bar"
    assert gate._dotted("not_python.txt") == "not_python.txt"


def _git(*args, cwd):
    # Suppressed once here rather than at every call site below: `git` is
    # PATH-resolved intentionally (test fixture setup, not user input) and
    # args are fixed literals from this file, never untrusted.
    subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", *args],  # noqa: S607  # PATH-resolved `git` is intentional
        cwd=cwd,
        check=True,
    )


def _ruff_bin():
    """The ruff the gate would reach, or None. Not pawl-specific: every
    deployment pins ruff as a dev dependency, but guard anyway so a checkout
    with no synced venv skips rather than fails."""
    root = paths.repo_root(os.getcwd())
    if root is not None:
        local = os.path.join(root, ".venv", "bin", "ruff")
        if os.path.exists(local):
            return local
    return shutil.which("ruff")


def _ruff(argv, cwd):
    return subprocess.run(  # noqa: S603  # argv is built here from a resolved ruff path plus literals
        argv, cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.mark.skipif(_ruff_bin() is None, reason="no ruff binary to exercise")
def test_gate_ruff_check_does_not_trust_a_stale_cache(tmp_path):
    """A ruff verdict can depend on files *other* than the one being linted:
    isort classifies `pkg.mod` first- or third-party by whether `pkg/mod`
    exists on disk. Ruff's per-file cache key is (mtime, permission mode)
    only -- not content, not that topology -- so creating the module does not
    invalidate the cached verdict for an untouched importer. A gate that reads
    that cache certifies code it would otherwise reject; measured 2026-08-02
    in a downstream deployment, where it passed 19 commits over two files that
    a cold run rejected. So the gate's ruff argv must not consult the cache.
    """
    ruff = _ruff_bin()
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["I"]\n')
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "present.py").write_text("A = 1\n")
    # Correctly sorted *while* pkg/absent.py does not exist: `pkg.absent` is
    # unresolvable, so isort files it third-party, above the first-party block.
    (tmp_path / "importer.py").write_text(
        "from pkg.absent import B\n\nfrom pkg.present import A\n\nprint(A, B)\n"
    )

    warm = _ruff([ruff, "check", "."], tmp_path)
    assert warm.returncode == 0, f"scenario is wrong, not the gate: {warm.stdout}"

    (pkg / "absent.py").write_text("B = 2\n")  # importer.py is NOT touched

    # The hazard itself. If this ever returns non-zero, ruff has started
    # keying the cache on something that catches this, and --no-cache below
    # has become redundant rather than load-bearing.
    assert _ruff([ruff, "check", "."], tmp_path).returncode == 0

    row = next(r for r in tools.GATE if r[0] == "ruff-check")
    flags = list(row[1][row[1].index("check") + 1 :])
    assert _ruff([ruff, "check", *flags], tmp_path).returncode != 0


def test_staged_files_lists_real_git_index(tmp_path):
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "mech").mkdir()
    (tmp_path / "mech" / "x.py").write_text("x = 1\n")
    _git("add", "mech/x.py", cwd=tmp_path)
    assert gate._staged_files(str(tmp_path)) == ["mech/x.py"]
