import io
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mech import baseline, ci_push, ci_weekly, mutation, paths, source_inventory, tools

HDR = baseline.HEADER + "\n" + baseline.DIVIDER + "\n"

_CI = {slot: (argv, timeout) for slot, argv, timeout in tools.CI}

COV = {
    "files": {
        "mech/paths.py": {"summary": {"covered_lines": 10, "num_statements": 12}},
        "mech/dead.py": {"summary": {"covered_lines": 0, "num_statements": 8}},
        "mech/empty.py": {"summary": {"covered_lines": 0, "num_statements": 0}},
    }
}
MODULES = ["mech/paths.py", "mech/dead.py", "mech/empty.py"]


@pytest.fixture(autouse=True)
def _tests_own_the_github_event_environment(monkeypatch):
    """A real push event must not steer nested ci_push unit invocations."""
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)


def test_coverage_verdict_flags_zero_covered_module():  # B-26
    assert ci_push.coverage_verdict(COV, MODULES) == ["mech/dead.py"]  # empty excluded


def test_coverage_absent_source_module_is_mutation_untestable():  # B-26 REQ-CI-1b
    assert ci_push.coverage_verdict(COV, MODULES + ["mech/ghost.py"]) == [
        "mech/ghost.py",
        "mech/dead.py",
    ]


def test_coverage_verdict_missing_keys_default_safely():  # mutation hardening
    cov = {
        "files": {
            "mech/nosum.py": {},  # no "summary" key at all
            "mech/nostat.py": {"summary": {"covered_lines": 0}},  # no num_statements
            "mech/nocov.py": {"summary": {"num_statements": 5}},  # no covered_lines
            "mech/onestmt.py": {"summary": {"num_statements": 1, "covered_lines": 0}},
        }
    }
    modules = ["mech/nosum.py", "mech/nostat.py", "mech/nocov.py", "mech/onestmt.py"]
    assert ci_push.coverage_verdict(cov, modules) == [
        "mech/nocov.py",
        "mech/onestmt.py",
    ]


def test_coverage_verdict_missing_files_key_treats_all_as_absent():  # hardening
    assert ci_push.coverage_verdict({}, ["mech/x.py"]) == ["mech/x.py"]


def test_discover_source_modules_finds_py_files_recursively(
    tmp_path,
):  # mutation hardening
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.mutmut]
source_paths = ["pkg"]
do_not_mutate = ["pkg/tests/*"]
""",
        encoding="utf-8",
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "b.py").write_text("y = 1\n")
    (tmp_path / "pkg" / "tests").mkdir()
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("assert True\n")
    (tmp_path / "pkg" / "notes.txt").write_text("skip me\n")
    (tmp_path / "outside.py").write_text("z = 1\n")
    assert ci_push._discover_source_modules(str(tmp_path)) == [
        "pkg/a.py",
        "pkg/sub/b.py",
    ]


def test_already_posted_finds_run_id_in_body_and_comments():  # B-36 idempotency
    issues = [
        {"number": 1, "body": "run-id: 41\n...", "comments": [{"body": "run-id: 42\n"}]}
    ]
    assert ci_weekly.already_posted(issues, "42") is True
    assert ci_weekly.already_posted(issues, "41") is True
    assert ci_weekly.already_posted(issues, "43") is False


def test_already_posted_rejects_substring_prefix_collision():  # REQ-CI-3 exact line
    issues = [{"number": 1, "body": "run-id: 41\n", "comments": []}]
    assert ci_weekly.already_posted(issues, "4") is False  # "4" in "run-id: 41"?


def test_already_posted_matches_exact_line_ignoring_trailing_whitespace():
    issues = [{"number": 1, "body": "run-id: 4  \n", "comments": []}]
    assert ci_weekly.already_posted(issues, "4") is True


def test_weekly_delta_retire_candidates():  # B-27
    rows = baseline.parse(
        HDR + "| m_env | environment-equivalent | fs case | macOS case-insensitive fs "
        "| 444444444444 |\n" + "| m_gone | equivalent | alias | | 555555555555 |\n"
    )
    states = {"killed": ["m_env", "other"], "survived": ["fresh_1"]}
    delta = ci_weekly.build_delta(states, rows, {"m_env": "444444444444"})
    assert delta == {
        "new_survivors": ["fresh_1"],
        # m_env is environment-equivalent and the weekly runs on the CI host, so
        # its kill confirms the row rather than obsoleting it (REQ-B-3).
        "retire_satisfied": [],
        "host_divergent": ["m_env"],
        "retire_obsolete": ["m_gone"],  # not generated at all
        "inconclusive": [],
        "fingerprint_mismatch": [],
        "errors": [],
    }


def test_weekly_delta_env_equivalent_kill_is_confirmation_not_retirement():
    """Verified 2026-07-25 against the real Linux run: 19 environment-equivalent
    rows were killed on ext4 while still surviving on the macOS measurement host.
    Filing those as retire-as-satisfied would remove suppressions this host still
    needs — the local gate would then block on them, the honest classification
    would be environment-equivalent again, and the row would oscillate."""
    rows = baseline.parse(
        HDR + "| m_case | environment-equivalent | GIT resolves here "
        "| reopens on a case-sensitive filesystem | aaaaaaaaaaaa |\n"
    )
    delta = ci_weekly.build_delta(
        {"killed": ["m_case"]}, rows, {"m_case": "aaaaaaaaaaaa"}
    )
    assert delta["host_divergent"] == ["m_case"]
    assert delta["retire_satisfied"] == []


def test_weekly_delta_equivalent_kill_is_still_retirement():
    """The sibling case must not be swept up: an `equivalent` row claims to be
    unkillable everywhere, so a kill disproves it outright."""
    rows = baseline.parse(
        HDR + "| m_plain | equivalent | claims unkillable |  | bbbbbbbbbbbb |\n"
    )
    delta = ci_weekly.build_delta(
        {"killed": ["m_plain"]}, rows, {"m_plain": "bbbbbbbbbbbb"}
    )
    assert delta["retire_satisfied"] == ["m_plain"]
    assert delta["host_divergent"] == []


def test_weekly_delta_no_tests_is_inconclusive_not_satisfied():
    """`no tests`, `not checked`, `skipped` and crash states are all "not
    survived", but none of them establishes a kill — a mutant nothing ever
    exercised must not tell the operator to retire its row."""
    rows = baseline.parse(
        HDR + "| m_unrun | equivalent | claims unkillable |  | cccccccccccc |\n"
    )
    delta = ci_weekly.build_delta(
        {"no tests": ["m_unrun"]}, rows, {"m_unrun": "cccccccccccc"}
    )
    assert delta["inconclusive"] == ["m_unrun"]
    assert delta["retire_satisfied"] == []
    assert delta["retire_obsolete"] == []


def test_weekly_red_on_inconclusive_alone():
    """An unverifiable baseline row is a failed verification, not
    housekeeping: staying green here would absorb the same silence the
    kill-definition fix just removed, one level up."""
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": [],
                "retire_satisfied": [],
                "retire_obsolete": [],
                "fingerprint_mismatch": [],
                "inconclusive": ["m_unrun"],
                "pip_audit": [],
            }
        )
        is True
    )


def test_weekly_stays_green_on_host_divergence_alone():
    """A confirmed host-dependent row is information, not a defect."""
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": [],
                "retire_satisfied": [],
                "retire_obsolete": [],
                "host_divergent": ["m_case"],
                "fingerprint_mismatch": [],
                "pip_audit": [],
            }
        )
        is False
    )


def test_weekly_delta_flags_a_repointed_surviving_row():  # REQ-B-4 / REQ-B-3 (c)
    """The bucket the existing three structurally cannot see: the row is
    generated, it did survive, and it is in the baseline — so it is neither
    retire-as-satisfied nor retire-as-obsolete nor a new survivor. Without this
    it keeps suppressing a mutation nobody described, silently and forever."""
    rows = baseline.parse(
        HDR + "| m_drift | equivalent | claims the old mutation |  | aaaaaaaaaaaa |\n"
    )
    states = {"survived": ["m_drift"]}
    delta = ci_weekly.build_delta(states, rows, {"m_drift": "bbbbbbbbbbbb"})
    assert delta["fingerprint_mismatch"] == ["m_drift"]
    assert delta["retire_satisfied"] == []
    assert delta["retire_obsolete"] == []
    assert delta["new_survivors"] == []


def test_weekly_delta_does_not_double_report_an_ungenerated_row():
    """An ungenerated row has no fingerprint to compare, so a naive mismatch
    check would list every retire-as-obsolete row twice."""
    rows = baseline.parse(HDR + "| m_gone | equivalent | alias |  | 555555555555 |\n")
    delta = ci_weekly.build_delta({"killed": ["other"]}, rows, {})
    assert delta["retire_obsolete"] == ["m_gone"]
    assert delta["fingerprint_mismatch"] == []


def test_weekly_red_on_fingerprint_mismatch_alone():
    """Unlike the retire buckets, a mismatch is not housekeeping — the row is
    misrepresenting what it guards, so it carries the same weight as a new
    unclassified survivor."""
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": [],
                "retire_satisfied": [],
                "retire_obsolete": [],
                "fingerprint_mismatch": ["m_drift"],
                "pip_audit": [],
            }
        )
        is True
    )


def test_weekly_red_only_on_survivors(monkeypatch):  # B-27, B-36
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": [],
                "retire_satisfied": ["x"],
                "retire_obsolete": [],
                "pip_audit": [],
            }
        )
        is False
    )
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": ["s"],
                "retire_satisfied": [],
                "retire_obsolete": [],
                "pip_audit": [],
            }
        )
        is True
    )
    assert (
        ci_weekly.job_red(
            {
                "new_survivors": [],
                "retire_satisfied": [],
                "retire_obsolete": [],
                "pip_audit": [{"id": "CVE-1"}],
            }
        )
        is True
    )


def test_issue_plan_zero_one_many():  # B-36
    assert ci_weekly.issue_plan([], delta_empty=False) == "create"
    assert ci_weekly.issue_plan([{"number": 1}], delta_empty=False) == "comment"
    assert (
        ci_weekly.issue_plan([{"number": 1}, {"number": 2}], delta_empty=False)
        == "fail_duplicates"
    )
    assert ci_weekly.issue_plan([], delta_empty=True) == "none"


def test_ci_entry_points_use_the_authoritative_scrub_policy(monkeypatch):
    """CI used three-entry copies after the gate policy grew to include the
    GIT_* and UV_* redirectors. A CI process with one of those names set could
    therefore certify a different repository or interpreter than its cwd."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    monkeypatch.setenv("GIT_DIR", "redirected/repository")
    monkeypatch.setenv("UV_WORKING_DIR", "redirected/project")
    monkeypatch.setenv("KEEP_ME", "1")

    expected = tools.scrubbed_env()
    assert ci_push._scrubbed_env() == expected
    assert ci_weekly._scrubbed_env() == expected
    assert expected["KEEP_ME"] == "1"


def test_tool_step_wires_argv_root_timeout_and_env(tmp_path):  # mutation hardening
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    error = ci_push._tool_step(["echo", "hi"], str(tmp_path), "step", fake_run, 42)

    assert error is None
    argv, kw = calls[0]
    assert argv == ["echo", "hi"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["timeout"] == 42
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert kw["check"] is False
    assert isinstance(kw["env"], dict) and kw["env"]  # _scrubbed_env(), never None


def test_tool_step_no_tests_is_error_only_on_exit5():  # mutation hardening
    def _exit5(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 5, stdout="", stderr="")

    error = ci_push._tool_step(["x"], ".", "pytest", _exit5, 10, no_tests_is_error=True)
    assert error == "pytest collected no tests — infrastructure error (fail-closed)"

    def _exit6(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 6, stdout="out", stderr="err")

    error6 = ci_push._tool_step(
        ["x"], ".", "pytest", _exit6, 10, no_tests_is_error=True
    )
    assert error6 == "pytest failed:\nouterr"

    def _exit5_flag_off(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 5, stdout="out", stderr="err")

    error_off = ci_push._tool_step(
        ["x"], ".", "pytest", _exit5_flag_off, 10, no_tests_is_error=False
    )
    assert error_off == "pytest failed:\nouterr"


def test_tool_step_exception_is_fail_closed():  # mutation hardening
    def boom(argv, **kw):  # noqa: ARG001  # kw unused; scripted crash
        raise OSError("tool missing")

    error = ci_push._tool_step(["x"], ".", "toolname", boom, 10)
    assert error == "toolname could not run: OSError: tool missing"


def test_tool_step_pass_returns_none():  # mutation hardening
    def ok(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    assert ci_push._tool_step(["x"], ".", "toolname", ok, 10) is None


def test_tool_step_no_tests_is_error_defaults_to_false():  # mutation hardening
    def _exit5(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 5, stdout="out", stderr="err")

    # no_tests_is_error omitted entirely — must default to False
    error = ci_push._tool_step(["x"], ".", "pytest", _exit5, 10)
    assert error == "pytest failed:\nouterr"


def test_tool_step_truncates_stdout_and_stderr_to_exact_windows():  # mutation hardening
    stdout, stderr = "Q" * 3001, "Z" * 1001  # chars absent from the surrounding text

    def _run(argv, **kw):  # noqa: ARG001  # kw unused; scripted outcome
        return subprocess.CompletedProcess(argv, 1, stdout=stdout, stderr=stderr)

    error = ci_push._tool_step(["x"], ".", "toolname", _run, 10)
    assert error is not None
    assert error == f"toolname failed:\n{stdout[-3000:]}{stderr[-1000:]}"
    assert error.count("Q") == 3000
    assert error.count("Z") == 1000


def test_canonical_issues_ignores_stray_pull_request_keys():  # gh issue list never
    # returns PRs (validated live against gh 2.94.0 on 2026-07-23:
    # `isPullRequest` isn't even a valid --json field for `gh issue list`) —
    # any stray isPullRequest/pull_request keys on an issue dict must not be
    # treated as an exclusion signal.
    issues = [{"number": 1, "isPullRequest": True, "pull_request": {"url": "x"}}]
    assert [i["number"] for i in ci_weekly.canonical_issues(issues)] == [1]


def test_canonical_issues_continues_scanning_past_exclusions():  # mutation hardening
    issues = [
        {"number": 2, "labels": [{"name": "other"}]},  # excluded: label mismatch
        {"number": 3, "labels": [{"name": "mech-weekly"}]},  # canonical
    ]
    assert [i["number"] for i in ci_weekly.canonical_issues(issues)] == [3]


def test_body_renders_exact_sections_and_values():  # mutation hardening
    delta = {
        "new_survivors": ["s1"],
        "retire_satisfied": ["r1"],
        "retire_obsolete": ["o1"],
        "host_divergent": ["h1"],
        "inconclusive": ["i1"],
        "fingerprint_mismatch": ["f1"],
        "errors": ["e1"],
        "pip_audit": [{"name": "pkg", "id": "CVE-1"}],
    }
    body = ci_weekly._body("123", delta)
    assert body == (
        "run-id: 123\n\n"
        "## new unclassified survivors\n['s1']\n\n"
        "## retire-as-satisfied candidates\n['r1']\n\n"
        "## retire-as-obsolete candidates\n['o1']\n\n"
        "## host-divergent rows (killed on CI host, condition confirmed)\n['h1']\n\n"
        "## inconclusive rows (generated, neither killed nor surviving — "
        "verify by hand)\n['i1']\n\n"
        "## fingerprint mismatches (retire and re-add)\n['f1']\n\n"
        "## step errors (action items)\n['e1']\n\n"
        "## pip-audit\n['pkg']\n"
    )


def test_body_defaults_errors_and_pip_audit_when_absent():  # mutation hardening
    delta = {"new_survivors": [], "retire_satisfied": [], "retire_obsolete": []}
    body = ci_weekly._body("x", delta)
    assert "## step errors (action items)\n[]\n\n" in body
    assert "## pip-audit\n[]\n" in body
    assert "## fingerprint mismatches (retire and re-add)\n[]\n\n" in body
    assert (
        "## host-divergent rows (killed on CI host, condition confirmed)\n[]\n\n"
        in body
    )
    assert (
        "## inconclusive rows (generated, neither killed nor surviving — "
        "verify by hand)\n[]\n\n" in body
    )


# --- Cross-review additions (mandatory, test-first) -------------------------


def _init_repo(base) -> str:
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved `git`; test fixture only
        cwd=base,
        check=True,
        capture_output=True,
    )
    return str(base)


Outcome = tuple[int, str | bytes, str | bytes]


def _name_status(*entries: tuple[str, ...]) -> bytes:
    """Encode the wire form of `git diff --name-status -z`."""
    fields = [os.fsencode(field) for entry in entries for field in entry]
    return b"\0".join(fields) + (b"\0" if fields else b"")


class ScriptedRun:
    """argv-matching `run` stub: rules are checked in order; the first whose
    predicate matches supplies the outcome (an (rc, stdout, stderr) tuple, or
    a callable `(argv, **kw) -> (rc, stdout, stderr)` for side-effecting
    steps). An unmatched argv raises — this is what lets a short-circuit
    test prove a later step was never invoked."""

    def __init__(
        self,
        rules: "list[tuple[Callable[[list], bool], Outcome | Callable[..., Outcome]]]",
    ):
        self.rules = rules
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
        for predicate, outcome in self.rules:
            if not predicate(argv):
                continue
            result: Outcome = outcome(argv, **kw) if callable(outcome) else outcome
            rc, out, err = result
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)
        raise AssertionError(f"unscripted call (short-circuit violation?): {argv}")


def _has(*tokens):
    """Contiguous-subsequence match — "coverage run" must not accidentally
    match a "coverage json" argv just because both contain "run" (uv run)."""
    n = len(tokens)

    def _match(argv):
        return any(tuple(argv[i : i + n]) == tokens for i in range(len(argv) - n + 1))

    return _match


def _is_collect_only(argv):
    return argv == [
        arg.replace("{module}", mutation.COLLECT_ONLY_FILTER)
        for arg in tools.MUTMUT_RUN
    ]


def _write_coverage_json(argv, **kw):  # noqa: ARG001  # argv unused; scripted outcome
    # Reports every real mech/*.py file under the scripted cwd as fully
    # covered, so tests that create their own fixture modules under mech/
    # don't spuriously trip the REQ-CI-1b mutation-untestable check.
    cwd = kw["cwd"]
    files = {}
    mech_dir = os.path.join(cwd, "mech")
    for dirpath, _dirs, names in os.walk(mech_dir):
        for name in names:
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, name), cwd).replace(
                    os.sep, "/"
                )
                files[rel] = {"summary": {"num_statements": 1, "covered_lines": 1}}
    cov_dir = os.path.join(cwd, ".mech")
    os.makedirs(cov_dir, exist_ok=True)
    with open(os.path.join(cov_dir, "coverage.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"files": files}))
    return 0, "", ""


def _all_pass_prefix():
    return [
        (_is_collect_only, (0, "", "")),
        (_has("mech.validation"), (0, "", "")),
        (_has("ruff", "format"), (0, "", "")),
        (_has("ruff", "check"), (0, "", "")),
        (_has("pyright"), (0, "", "")),
        (_has("coverage", "run"), (0, "", "")),
        (_has("coverage", "json"), _write_coverage_json),
    ]


def test_ci_push_main_short_circuits_and_exit1(tmp_path, capsys):  # B-24 wire
    root = _init_repo(tmp_path)
    fake = ScriptedRun([(_has("mech.validation"), (1, "", "registration drift"))])

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert fake.calls == [
        list(_CI["ci-validation"][0])
    ]  # nothing after the first failure
    assert "registration drift" in capsys.readouterr().err


def test_ci_push_main_all_pass_returns_0(tmp_path, capsys):  # full happy path
    root = _setup_repo(tmp_path)
    fake = ScriptedRun(_all_pass_prefix())

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    # every gate tool actually ran — a green push that skipped its steps and a
    # green push that passed them must not look the same (spec §12)
    for probe in (_has("mech.validation"), _has("ruff", "check"), _has("pyright")):
        assert any(probe(c) for c in fake.calls)


# --- ci_push.main: one exact-assertion test per branch (mutation hardening) -


def _setup_repo(tmp_path, select=("E", "F")):
    root = _init_repo(tmp_path)
    (tmp_path / "mech").mkdir(exist_ok=True)
    (tmp_path / "mech" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.ruff.lint]\nselect = {list(select)!r}\n"
        '\n[tool.mutmut]\nsource_paths = ["mech"]\n'
    )
    (tmp_path / "MUTANTS.md").write_text(HDR)
    return root


def test_ci_push_each_lint_step_short_circuits_with_exact_message(tmp_path, capsys):
    order = ("ci-validation", "ci-ruff-format", "ci-ruff-check", "ci-pyright")
    for slot, message in [
        ("ci-ruff-format", "not formatted"),
        ("ci-ruff-check", "F401 unused"),
        ("ci-pyright", "type error"),
    ]:
        sub = tmp_path / slot
        sub.mkdir()
        root = _init_repo(sub)
        rules = []
        for prior_slot in order:
            if prior_slot == slot:
                break
            rules.append((_has(*_CI[prior_slot][0][2:]), (0, "", "")))
        rules.append((_has(*_CI[slot][0][2:]), (1, message, "")))
        fake = ScriptedRun(rules)

        code = ci_push.main(root=root, run=fake)

        assert code == 1
        assert f"{slot} failed" in capsys.readouterr().err
        assert (
            len(fake.calls) == order.index(slot) + 1
        )  # nothing after the failing step


def test_ci_push_coverage_run_exit5_is_no_tests_error(tmp_path, capsys):
    root = _init_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mech.validation"), (0, "", "")),
            (_has("ruff", "format"), (0, "", "")),
            (_has("ruff", "check"), (0, "", "")),
            (_has("pyright"), (0, "", "")),
            (_has("coverage", "run"), (5, "", "")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert "ci-coverage-run collected no tests" in capsys.readouterr().err
    assert not any(_has("coverage", "json")(c) for c in fake.calls)


def test_ci_push_only_coverage_run_treats_exit5_as_no_tests(tmp_path, capsys):
    order = ("ci-validation", "ci-ruff-format", "ci-ruff-check", "ci-pyright")
    for slot in order:
        sub = tmp_path / f"exit5-{slot}"
        sub.mkdir()
        root = _init_repo(sub)
        rules = []
        for prior_slot in order:
            if prior_slot == slot:
                break
            rules.append((_has(*_CI[prior_slot][0][2:]), (0, "", "")))
        rules.append((_has(*_CI[slot][0][2:]), (5, "out", "err")))
        fake = ScriptedRun(rules)

        code = ci_push.main(root=root, run=fake)

        assert code == 1
        err = capsys.readouterr().err
        assert f"{slot} failed" in err  # generic failure, not the no-tests message
        assert "collected no tests" not in err


def test_ci_push_lint_loop_passes_real_root_and_timeout_to_each_step(tmp_path):
    root = _init_repo(tmp_path)
    calls = []

    # let coverage-run be the final (5th) call so main() short-circuits there
    # without needing the coverage-json/event machinery.
    def _stop_at_coverage_run(argv, **kw):
        calls.append((argv, kw))
        if _has("coverage", "run")(argv):
            return subprocess.CompletedProcess(argv, 1, stdout="stop here", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ci_push.main(root=root, run=_stop_at_coverage_run)

    assert len(calls) == 5
    slots = (
        "ci-validation",
        "ci-ruff-format",
        "ci-ruff-check",
        "ci-pyright",
        "ci-coverage-run",
    )
    for (_argv, kw), slot in zip(calls, slots, strict=True):
        assert kw["cwd"] == root
        assert kw["timeout"] == _CI[slot][1]


def test_ci_push_coverage_json_call_wires_kwargs_and_truncates(tmp_path, capsys):
    root = _init_repo(tmp_path)
    stdout, stderr = "Q" * 3001, "Z" * 1001

    def _cov_json_fail(argv, **kw):  # noqa: ARG001  # argv unused; scripted outcome
        return 1, stdout, stderr

    fake = ScriptedRun(
        [
            *_all_pass_prefix()[:-1],
            (_has("coverage", "run"), (0, "", "")),
            (_has("coverage", "json"), _cov_json_fail),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert err.strip() == f"ci-coverage-json failed:\n{stdout[-3000:]}{stderr[-1000:]}"
    assert err.count("Q") == 3000
    assert err.count("Z") == 1000
    argv, kw = fake.calls[-1], fake.kwargs[-1]
    assert kw["cwd"] == root
    argv_template, timeout = _CI["ci-coverage-json"]
    assert argv == list(argv_template)
    assert kw["timeout"] == timeout
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert kw["check"] is False
    assert isinstance(kw["env"], dict) and kw["env"]


def test_ci_push_coverage_json_step_failure_exact_message(tmp_path, capsys):
    root = _init_repo(tmp_path)
    fake = ScriptedRun(
        [
            *_all_pass_prefix()[:-1],
            (_has("coverage", "run"), (0, "", "")),
            (_has("coverage", "json"), (1, "boom-out", "boom-err")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert "ci-coverage-json failed" in err and "boom-out" in err and "boom-err" in err


def test_ci_push_coverage_json_malformed_is_unreadable_error(tmp_path, capsys):
    root = _init_repo(tmp_path)

    def _write_bad_json(argv, **kw):  # noqa: ARG001  # argv unused; scripted outcome
        cov_dir = os.path.join(kw["cwd"], ".mech")
        os.makedirs(cov_dir, exist_ok=True)
        with open(os.path.join(cov_dir, "coverage.json"), "w", encoding="utf-8") as fh:
            fh.write("not json")
        return 0, "", ""

    fake = ScriptedRun(
        [
            *_all_pass_prefix()[:-1],
            (_has("coverage", "run"), (0, "", "")),
            (_has("coverage", "json"), _write_bad_json),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert "coverage.json unreadable" in err
    assert "JSONDecodeError" in err  # the real exception's class name, not "NoneType"


def test_ci_push_coverage_verdict_uncovered_module_blocks(tmp_path, capsys):
    root = _init_repo(tmp_path)
    (tmp_path / "mech").mkdir()
    (tmp_path / "mech" / "dead.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["mech"]\n')

    def _write_cov(argv, **kw):  # noqa: ARG001  # argv unused; scripted outcome
        cov_dir = os.path.join(kw["cwd"], ".mech")
        os.makedirs(cov_dir, exist_ok=True)
        report = {
            "files": {
                "mech/dead.py": {"summary": {"num_statements": 3, "covered_lines": 0}}
            }
        }
        with open(os.path.join(cov_dir, "coverage.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report))
        return 0, "", ""

    fake = ScriptedRun(
        [
            *_all_pass_prefix()[:-1],
            (_has("coverage", "run"), (0, "", "")),
            (_has("coverage", "json"), _write_cov),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert "mutation-untestable: mech/dead.py" in capsys.readouterr().err
    assert not any("GITHUB_EVENT_PATH" in "".join(c) for c in fake.calls)


def test_ci_push_null_event_json_does_not_crash_and_skips_diff(
    tmp_path, monkeypatch, capsys
):  # literal JSON `null` parses cleanly to None — must not enter the event branch
    root = _setup_repo(tmp_path)
    event_path = tmp_path / "event.json"
    event_path.write_text("null")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    fake = ScriptedRun([*_all_pass_prefix()])

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert not any("--name-status" in c for c in fake.calls)


def test_diff_base_default_push_uses_event_before():
    assert ci_push.diff_base("abc123", "refs/heads/main", "main") == {
        "mode": "scoped",
        "base": "abc123",
    }


def test_diff_base_feature_push_uses_merge_base():
    assert ci_push.diff_base("ignored", "refs/heads/feature", "main") == {
        "mode": "scoped",
        "base": "merge-base",
    }


def test_diff_base_new_default_branch_runs_unscoped():
    assert ci_push.diff_base("0" * 40, "refs/heads/main", "main") == {
        "mode": "unscoped",
        "base": None,
    }


def test_mutation_env_forces_xdist_off_without_preserving_ambient_addopts(
    monkeypatch,
):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n auto -k nothing")
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)

    env = mutation.execution_env()

    assert env["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0"


def test_mutation_env_does_not_invent_xdist_option_without_plugin(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: False)

    env = mutation.execution_env()

    assert "PYTEST_ADDOPTS" not in env


def test_xdist_config_guard_accepts_ordinary_mutmut_pytest_args(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.mutmut]
source_paths = ["pkg"]
pytest_add_cli_args = ["-p", "no:randomly"]
pytest_add_cli_args_test_selection = ["tests/"]
"""
    )

    assert mutation.xdist_config_error(str(tmp_path)) is None


@pytest.mark.parametrize(
    "args",
    [
        '["-n", "auto"]',
        '["-n=4"]',
        '["-n4"]',
        '["-nauto"]',
        '["-nlogical"]',
        '["-d"]',
        '["--numprocesses"]',
        '["--numprocesses=logical"]',
        '["--dist", "loadscope"]',
        '["--dist=loadscope"]',
        '["--tx"]',
        '["--tx=popen//python=python3"]',
        '["-p", "no:xdist"]',
    ],
)
@pytest.mark.parametrize(
    "key", ["pytest_add_cli_args", "pytest_add_cli_args_test_selection"]
)
def test_xdist_config_guard_rejects_later_explicit_worker_controls(tmp_path, args, key):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.mutmut]\nsource_paths = ["pkg"]\n{key} = {args}'
    )

    error = mutation.xdist_config_error(str(tmp_path))

    assert error is not None
    assert "Pawl owns serial execution" in error


def test_xdist_config_guard_rejects_invalid_argument_shape(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["pkg"]\npytest_add_cli_args = "-n0"\n'
    )

    assert mutation.xdist_config_error(str(tmp_path)) == (
        "mutation config unreadable: pytest_add_cli_args must contain strings"
    )


def test_mutmut_pytest_args_missing_file_names_real_exception(tmp_path):
    args, error = mutation.mutmut_pytest_args(str(tmp_path))

    assert args == []
    assert error is not None
    assert error.startswith("mutation config unreadable: FileNotFoundError:")
    assert "pyproject.toml" in error


def test_mutmut_pytest_args_requires_mutmut_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.other]\nvalue = true\n")

    assert mutation.mutmut_pytest_args(str(tmp_path)) == (
        [],
        "mutation config unreadable: [tool.mutmut] is missing",
    )


def _push_event(tmp_path, monkeypatch, **overrides):
    event = {
        "before": "abc123",
        "ref": "refs/heads/main",
        "repository": {"default_branch": "main"},
    }
    event.update(overrides)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))


def test_ci_push_mutates_only_changed_inventory_modules(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("def answer():\n    return 42\n")
    _push_event(tmp_path, monkeypatch)
    seen = {}

    def fake_run_scoped(modules, r, run, env):
        seen.update(modules=modules, root=r, run=run, env=env)
        return [], None

    monkeypatch.setattr(mutation, "run_scoped", fake_run_scoped)
    monkeypatch.setattr(mutation, "fingerprints", lambda ids, r: ({}, {}))
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (
                _has("git", "diff"),
                (0, _name_status(("M", "mech/app.py"), ("M", "README.md")), b""),
            ),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert seen["modules"] == ["mech.app"]
    assert seen["root"] == root
    assert seen["run"] is fake
    assert seen["env"]["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0"
    diff_index = next(
        i for i, call in enumerate(fake.calls) if _has("git", "diff")(call)
    )
    assert fake.kwargs[diff_index]["cwd"] == root
    assert "mutated mech.app" in capsys.readouterr().out


def test_ci_push_docs_only_diff_skips_mutation(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    _push_event(tmp_path, monkeypatch)

    def should_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("docs-only push invoked mutation")

    monkeypatch.setattr(mutation, "run_scoped", should_not_run)
    monkeypatch.setattr(mutation, "regenerate_tree", should_not_run)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "README.md")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert capsys.readouterr().out == "ci_push: mutation skipped (no relevant diff)\n"


def test_ci_push_baseline_only_diff_regenerates_and_checks_every_row(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    row = ("mech.app.x_f__mutmut_1", "equivalent", "same behaviour", "", "a" * 12)
    (tmp_path / "MUTANTS.md").write_text(baseline.render([row]))
    _push_event(tmp_path, monkeypatch)
    seen = {}

    def fake_regenerate(r, run, env):
        seen.update(root=r, run=run, env=env)
        return None

    def fake_fingerprints(ids, r):
        seen.update(ids=list(ids), fingerprint_root=r)
        return {row[0]: row[4]}, {}

    monkeypatch.setattr(mutation, "regenerate_tree", fake_regenerate)
    monkeypatch.setattr(mutation, "fingerprints", fake_fingerprints)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "MUTANTS.md")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert seen["root"] == root
    assert seen["run"] is fake
    assert seen["ids"] == [row[0]]
    assert seen["fingerprint_root"] == root
    assert "tree regenerated" in capsys.readouterr().out


def test_ci_push_new_survivor_blocks(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("def answer():\n    return 42\n")
    _push_event(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutation,
        "run_scoped",
        lambda modules, r, run, env: (["mech.app.x_answer__mutmut_1"], None),
    )
    monkeypatch.setattr(mutation, "fingerprints", lambda ids, r: ({}, {}))
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert "new survivor: mech.app.x_answer__mutmut_1" in capsys.readouterr().err


def test_ci_push_checks_fingerprints_only_for_changed_modules(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("def answer():\n    return 42\n")
    rows = [
        ("mech.app.x_f__mutmut_1", "equivalent", "same", "", "a" * 12),
        ("mech.other.x_f__mutmut_1", "equivalent", "same", "", "b" * 12),
    ]
    (tmp_path / "MUTANTS.md").write_text(baseline.render(rows))
    _push_event(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(mutation, "run_scoped", lambda modules, r, run, env: ([], None))

    def fake_fingerprints(ids, r):
        seen["ids"] = list(ids)
        return {rows[0][0]: "c" * 12}, {}

    monkeypatch.setattr(mutation, "fingerprints", fake_fingerprints)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert seen["ids"] == [rows[0][0]]
    err = capsys.readouterr().err
    assert "baseline fingerprint mismatch" in err
    assert rows[0][0] in err
    assert rows[1][0] not in err


def test_xdist_probe_returns_the_import_spec_verdict(monkeypatch):
    calls = []

    def fake_find_spec(name):
        calls.append(name)
        return object()

    monkeypatch.setattr(mutation.importlib.util, "find_spec", fake_find_spec)
    assert mutation._xdist_installed() is True
    assert calls == ["xdist"]

    monkeypatch.setattr(mutation.importlib.util, "find_spec", lambda name: None)
    assert mutation._xdist_installed() is False


def test_all_inventory_modules_maps_exclusions_and_packages(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["pkg"]\ndo_not_mutate = ["pkg/tests/*"]\n'
    )
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "app.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "tests" / "test_app.py").write_text("assert True\n")
    inventory = source_inventory.load(str(tmp_path))

    assert ci_push._all_inventory_modules(inventory, str(tmp_path)) == [
        "pkg",
        "pkg.app",
    ]


def test_git_call_wires_exact_subprocess_contract_and_scrubs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    seen = {}
    expected = subprocess.CompletedProcess(["git", "diff"], 0, "M\tx.py\n", "")

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return expected

    proc, error = ci_push._git_call(["git", "diff"], str(tmp_path), fake_run, 17)

    assert proc is expected
    assert error is None
    assert seen == {
        "argv": ["git", "diff"],
        "kwargs": {
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 17,
            "check": False,
            "env": tools.scrubbed_env(),
        },
    }
    assert "PYTEST_ADDOPTS" not in seen["kwargs"]["env"]


def test_git_call_converts_exception_to_fail_closed_error(tmp_path):
    def exploding_run(argv, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(argv, 3)

    proc, error = ci_push._git_call(["git", "diff"], str(tmp_path), exploding_run, 3)

    assert proc is None
    expected = (
        "git diff discovery failed: TimeoutExpired: "
        "Command '['git', 'diff']' timed out after 3 seconds"
    )
    assert error == expected


def _scope_fixture(tmp_path):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("x = 1\n")
    (tmp_path / "mech" / "other.py").write_text("y = 2\n")
    return root, source_inventory.load(root)


def test_mutation_scope_maps_statuses_deduplicates_and_uses_exact_range(tmp_path):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [
            (
                _has("git", "diff"),
                (
                    0,
                    _name_status(
                        ("M", "README with spaces.md"),
                        ("M", "mech/app.py"),
                        ("A", "mech/app.py"),
                        ("R100", "mech/old.py", "mech/other.py"),
                    ),
                    b"",
                ),
            )
        ]
    )

    modules, baseline_changed, verify_all, error = ci_push._mutation_scope(
        {
            "before": "base123",
            "ref": "refs/heads/main",
            "repository": {"default_branch": "main"},
        },
        root,
        inventory,
        fake,
    )

    assert (modules, baseline_changed, verify_all, error) == (
        ["mech.app", "mech.other"],
        False,
        False,
        None,
    )
    assert fake.calls == [
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--relative",
            "--diff-filter=AMR",
            "base123...HEAD",
        ]
    ]
    assert fake.kwargs[0]["timeout"] == _CI["ci-git-diff"][1]
    assert fake.kwargs[0]["cwd"] == root


def test_mutation_scope_rebases_real_git_diff_to_nested_project(tmp_path):
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved git; fixture only
        cwd=tmp_path,
        check=True,
    )
    for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(  # noqa: S603  # fixed fixture inputs only
            ["git", "config", key, value],  # noqa: S607  # PATH-resolved git
            cwd=tmp_path,
            check=True,
        )
    project = tmp_path / "service"
    package = project / "pkg"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["pkg"]\n', encoding="utf-8"
    )
    module = package / "app.py"
    module.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"],  # noqa: S607  # fixture-only git staging
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "base"],  # noqa: S607  # fixture-only commit
        cwd=tmp_path,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607  # fixture-only lookup
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    module.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-qam", "change"],  # noqa: S607  # fixture-only commit
        cwd=tmp_path,
        check=True,
    )

    result = ci_push._mutation_scope(
        {
            "before": base,
            "ref": "refs/heads/main",
            "repository": {"default_branch": "main"},
        },
        str(project),
        source_inventory.load(str(project)),
        subprocess.run,
    )

    assert result == (["pkg.app"], False, False, None)


def test_mutation_scope_fails_closed_on_malformed_name_status_output(tmp_path):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun([(_has("git", "diff"), (0, b"M\0", b""))])

    result = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == (
        [],
        False,
        False,
        "git diff output is truncated after status 'M': fail closed",
    )


def test_name_status_entries_parses_add_modify_rename_and_empty():
    output = _name_status(
        ("A", "pkg/new.py"),
        ("M", "pkg/live.py"),
        ("R100", "pkg/old.py", "pkg/renamed.py"),
    )

    assert ci_push._name_status_entries(output) == (
        [
            ("A", ["pkg/new.py"]),
            ("M", ["pkg/live.py"]),
            ("R100", ["pkg/old.py", "pkg/renamed.py"]),
        ],
        None,
    )
    assert ci_push._name_status_entries(b"") == ([], None)


def test_name_status_entries_rejects_text_and_nonterminated_bytes_exactly():
    assert ci_push._name_status_entries("M\0pkg/live.py\0") == (
        [],
        "git diff output was not bytes: fail closed",
    )
    assert ci_push._name_status_entries(b"M\0pkg/live.py") == (
        [],
        "git diff output was not NUL-terminated: fail closed",
    )


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"M\0", "git diff output is truncated after status 'M': fail closed"),
        (
            b"X\0pkg/live.py\0",
            "git diff output has unexpected status 'X': fail closed",
        ),
        (b"M\0\0", "git diff output has an empty path for status 'M': fail closed"),
    ],
)
def test_name_status_entries_rejects_malformed_records(output, message):
    assert ci_push._name_status_entries(output) == ([], message)


def test_mutation_scope_reads_unquoted_unicode_path_from_nul_delimited_bytes(
    tmp_path,
):
    root, inventory = _scope_fixture(tmp_path)
    (tmp_path / "mech" / "café.py").write_text("value = 1\n", encoding="utf-8")
    fake = ScriptedRun([(_has("git", "diff"), (0, b"M\0mech/caf\xc3\xa9.py\0", b""))])

    result = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == (["mech.café"], False, False, None)
    assert "-z" in fake.calls[0]
    assert fake.kwargs[0]["text"] is False


@pytest.mark.parametrize("repository", [None, {}])
def test_mutation_scope_defaults_missing_repository_branch_to_main(
    tmp_path, repository
):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [(_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b""))]
    )
    event = {
        "before": "base",
        "ref": "refs/heads/main",
        "repository": repository,
    }

    result = ci_push._mutation_scope(event, root, inventory, fake)

    assert result == (["mech.app"], False, False, None)
    assert fake.calls[-1][-1] == "base...HEAD"


@pytest.mark.parametrize("event", [{}, {"before": "base"}])
def test_mutation_scope_missing_ref_uses_feature_merge_base(tmp_path, event):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [
            (_has("git", "merge-base"), (0, "merge\n", "")),
            (_has("git", "diff"), (0, b"", b"")),
        ]
    )

    result = ci_push._mutation_scope(event, root, inventory, fake)

    assert result == ([], False, False, None)
    assert fake.calls[0] == ["git", "merge-base", "HEAD", "origin/main"]


def test_mutation_scope_feature_branch_uses_merge_base_output(tmp_path):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [
            (_has("git", "merge-base"), (0, "merge456\n", "")),
            (
                _has("git", "diff"),
                (0, _name_status(("M", "mech/app.py")), b""),
            ),
        ]
    )

    result = ci_push._mutation_scope(
        {
            "before": "ignored",
            "ref": "refs/heads/feature",
            "repository": {"default_branch": "trunk"},
        },
        root,
        inventory,
        fake,
    )

    assert result == (["mech.app"], False, False, None)
    assert fake.calls == [
        ["git", "merge-base", "HEAD", "origin/trunk"],
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--relative",
            "--diff-filter=AMR",
            "merge456...HEAD",
        ],
    ]
    assert fake.kwargs[0]["cwd"] == root
    assert fake.kwargs[0]["timeout"] == _CI["ci-git-merge-base"][1]
    assert fake.kwargs[1]["cwd"] == root
    assert fake.kwargs[1]["timeout"] == _CI["ci-git-diff"][1]


@pytest.mark.parametrize(
    ("merge_result", "message"),
    [
        ((1, "", "fatal"), "merge base unavailable: fail closed"),
        ((0, "\n", ""), "merge base unavailable: fail closed"),
    ],
)
def test_mutation_scope_feature_branch_fails_closed_without_merge_base(
    tmp_path, merge_result, message
):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun([(_has("git", "merge-base"), merge_result)])

    result = ci_push._mutation_scope(
        {"ref": "refs/heads/feature", "repository": {"default_branch": "main"}},
        root,
        inventory,
        fake,
    )

    assert result == ([], False, False, message)
    assert len(fake.calls) == 1


def test_mutation_scope_fails_closed_when_diff_fails(tmp_path):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun([(_has("git", "diff"), (2, b"", b"bad revision"))])

    result = ci_push._mutation_scope(
        {"before": "badbase", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == (
        [],
        False,
        False,
        "diff base unavailable (badbase...HEAD): fail closed",
    )


@pytest.mark.parametrize("stage", ["merge", "diff"])
def test_mutation_scope_propagates_git_discovery_exception(
    tmp_path, monkeypatch, stage
):
    root, inventory = _scope_fixture(tmp_path)
    calls = []

    def fake_git_call(argv, r, run, timeout, *, text=True):  # noqa: ARG001
        calls.append((argv, r, timeout))
        if stage == "merge" or (stage == "diff" and len(calls) == 1):
            return None, "git exploded"
        return subprocess.CompletedProcess(argv, 0, "merge\n", ""), None

    monkeypatch.setattr(ci_push, "_git_call", fake_git_call)
    event = (
        {"ref": "refs/heads/feature"}
        if stage == "merge"
        else {"before": "base", "ref": "refs/heads/main"}
    )

    result = ci_push._mutation_scope(event, root, inventory, object())

    assert result == ([], False, False, "git exploded")
    assert calls[-1][1] == root
    expected_slot = "ci-git-merge-base" if stage == "merge" else "ci-git-diff"
    assert calls[-1][2] == _CI[expected_slot][1]


def test_mutation_scope_marks_baseline_only_without_mutating(tmp_path):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [(_has("git", "diff"), (0, _name_status(("M", "MUTANTS.md")), b""))]
    )

    result = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == ([], True, False, None)


@pytest.mark.parametrize(
    ("control_path", "expected"),
    [
        ("MUTANTS.md", ([], True, False, None)),
        (
            "pyproject.toml",
            (["mech", "mech.app", "mech.other"], False, True, None),
        ),
        ("uv.lock", (["mech", "mech.app", "mech.other"], False, True, None)),
    ],
)
def test_mutation_scope_rename_away_from_control_file_stays_relevant(
    tmp_path, control_path, expected
):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [
            (
                _has("git", "diff"),
                (0, _name_status(("R100", control_path, "notes.txt")), b""),
            )
        ]
    )

    result = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == expected


@pytest.mark.parametrize("control_path", ["pyproject.toml", "uv.lock"])
def test_mutation_scope_control_change_expands_to_every_inventory_module(
    tmp_path, control_path
):
    root, inventory = _scope_fixture(tmp_path)
    fake = ScriptedRun(
        [
            (
                _has("git", "diff"),
                (
                    0,
                    _name_status(("M", control_path), ("M", "mech/app.py")),
                    b"",
                ),
            )
        ]
    )

    result = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    assert result == (["mech", "mech.app", "mech.other"], False, True, None)


def test_mutation_scope_inventory_failure_preserves_scope_flags(tmp_path, monkeypatch):
    root, inventory = _scope_fixture(tmp_path)

    def fail_inventory(*args):  # noqa: ARG001
        raise source_inventory.InventoryError("missing configured root")

    monkeypatch.setattr(ci_push, "_all_inventory_modules", fail_inventory)

    unscoped = ci_push._mutation_scope(
        {"ref": "refs/heads/main"}, root, inventory, ScriptedRun([])
    )
    fake = ScriptedRun(
        [
            (
                _has("git", "diff"),
                (
                    0,
                    _name_status(("M", "MUTANTS.md"), ("M", "pyproject.toml")),
                    b"",
                ),
            )
        ]
    )
    configured = ci_push._mutation_scope(
        {"before": "base", "ref": "refs/heads/main"}, root, inventory, fake
    )

    message = "source inventory unreadable: missing configured root"
    assert unscoped == ([], False, True, message)
    assert configured == ([], True, True, message)


@pytest.mark.parametrize("before", [None, "0" * 40])
def test_mutation_scope_new_default_push_is_unscoped(tmp_path, before):
    root, inventory = _scope_fixture(tmp_path)

    result = ci_push._mutation_scope(
        {"before": before, "ref": "refs/heads/main"},
        root,
        inventory,
        ScriptedRun([]),
    )

    assert result == (["mech", "mech.app", "mech.other"], False, True, None)


def test_read_baseline_returns_validated_rows(tmp_path):
    row = ("pkg.x_f__mutmut_1", "equivalent", "same", "", "a" * 12)
    (tmp_path / "MUTANTS.md").write_text(baseline.render([row]))
    assert ci_push._read_baseline(str(tmp_path)) == ([row], None)


def test_read_baseline_names_parse_and_schema_failures(tmp_path):
    (tmp_path / "MUTANTS.md").write_text("wrong\n")
    rows, error = ci_push._read_baseline(str(tmp_path))
    assert rows == []
    assert error == "MUTANTS.md unreadable: BaselineError: missing or wrong header"

    bad = ("pkg.x_f__mutmut_1", "invented", "same", "", "a" * 12)
    (tmp_path / "MUTANTS.md").write_text(baseline.render([bad]))
    rows, error = ci_push._read_baseline(str(tmp_path))
    assert rows == []
    assert (
        error == "MUTANTS.md invalid row pkg.x_f__mutmut_1: unknown class: 'invented'"
    )


def test_read_baseline_missing_file_names_real_exception(tmp_path):
    rows, error = ci_push._read_baseline(str(tmp_path))
    assert rows == []
    assert error is not None
    assert error.startswith("MUTANTS.md unreadable: FileNotFoundError:")
    assert "MUTANTS.md" in error


def test_fingerprint_errors_reports_only_mismatches(monkeypatch):
    rows = [
        ("pkg.x_a__mutmut_1", "equivalent", "same", "", "a" * 12),
        ("pkg.x_b__mutmut_1", "equivalent", "same", "", "b" * 12),
        ("pkg.x_c__mutmut_1", "equivalent", "same", "", "c" * 12),
    ]
    monkeypatch.setattr(
        mutation,
        "fingerprints",
        lambda ids, root: (
            {rows[0][0]: rows[0][4], rows[1][0]: "d" * 12},
            {rows[2][0]: "no generated module"},
        ),
    )

    errors = ci_push._fingerprint_errors(rows, "/repo")

    mismatch = (
        f"baseline fingerprint mismatch: {rows[1][0]}: "
        f"expected {'b' * 12}, got {'d' * 12}"
    )
    assert errors == [
        mismatch,
        f"baseline fingerprint mismatch: {rows[2][0]}: no generated module",
    ]


def test_ci_push_mutation_adapter_error_blocks(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("x = 1\n")
    _push_event(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutation,
        "run_scoped",
        lambda modules, r, run, env: ([], "mutation subprocess failed"),
    )
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == "mutation subprocess failed\n"


def test_ci_push_xdist_config_conflict_blocks_before_mutation(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("x = 1\n")
    with (tmp_path / "pyproject.toml").open("a") as fh:
        fh.write('pytest_add_cli_args = ["-n", "auto"]\n')
    _push_event(tmp_path, monkeypatch)

    def should_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("unsafe xdist config reached mutation")

    monkeypatch.setattr(mutation, "run_scoped", should_not_run)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == (
        "mutation config may not control xdist in pytest_add_cli_args*: '-n'; "
        "Pawl owns serial execution\n"
    )


def test_ci_push_malformed_event_fails_with_real_exception(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    event_path = tmp_path / "event.json"
    event_path.write_text("{")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    code = ci_push.main(root=root, run=ScriptedRun(_all_pass_prefix()))

    assert code == 1
    assert "GITHUB_EVENT_PATH unreadable: JSONDecodeError:" in capsys.readouterr().err


def test_ci_push_source_inventory_error_is_exact(tmp_path, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["missing"]\n'
    )

    code = ci_push.main(root=root, run=ScriptedRun(_all_pass_prefix()))

    assert code == 1
    assert capsys.readouterr().err == (
        "source inventory unreadable: configured source path is missing: missing\n"
    )


def test_ci_push_diff_discovery_error_is_exact(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    _push_event(tmp_path, monkeypatch)
    fake = ScriptedRun(
        [*_all_pass_prefix(), (_has("git", "diff"), (2, b"", b"bad revision"))]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == (
        "diff base unavailable (abc123...HEAD): fail closed\n"
    )


def test_ci_push_invalid_baseline_blocks_before_mutation(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("x = 1\n")
    bad = ("mech.app.x_f__mutmut_1", "invented", "same", "", "a" * 12)
    (tmp_path / "MUTANTS.md").write_text(baseline.render([bad]))
    _push_event(tmp_path, monkeypatch)

    def should_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("invalid baseline reached mutation")

    monkeypatch.setattr(mutation, "run_scoped", should_not_run)
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == (
        "MUTANTS.md invalid row mech.app.x_f__mutmut_1: unknown class: 'invented'\n"
    )


def test_ci_push_multiple_modules_have_stable_scope_report(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "a.py").write_text("a = 1\n")
    (tmp_path / "mech" / "b.py").write_text("b = 2\n")
    _push_event(tmp_path, monkeypatch)
    monkeypatch.setattr(mutation, "run_scoped", lambda modules, r, run, env: ([], None))
    monkeypatch.setattr(mutation, "fingerprints", lambda ids, r: ({}, {}))
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (
                _has("git", "diff"),
                (
                    0,
                    _name_status(("M", "mech/b.py"), ("M", "mech/a.py")),
                    b"",
                ),
            ),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert capsys.readouterr().out == (
        "ci_push: verified 0 baseline row(s); mutated mech.a, mech.b; "
        "0 new survivor(s)\n"
    )


def test_ci_push_baseline_regeneration_uses_mutation_env_and_exact_report(
    tmp_path, monkeypatch, capsys
):
    root = _setup_repo(tmp_path)
    _push_event(tmp_path, monkeypatch)
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    seen = {}

    def fake_regenerate(r, run, env):
        seen.update(root=r, run=run, env=env)
        return None

    monkeypatch.setattr(mutation, "regenerate_tree", fake_regenerate)
    monkeypatch.setattr(mutation, "fingerprints", lambda ids, r: ({}, {}))
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "MUTANTS.md")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert seen["root"] == root
    assert seen["run"] is fake
    assert seen["env"]["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0"
    assert capsys.readouterr().out == (
        "ci_push: verified 0 baseline row(s); tree regenerated; 0 new survivor(s)\n"
    )


def test_ci_push_accepts_a_listed_survivor(tmp_path, monkeypatch, capsys):
    root = _setup_repo(tmp_path)
    (tmp_path / "mech" / "app.py").write_text("x = 1\n")
    row = ("mech.app.x_f__mutmut_1", "equivalent", "same", "", "a" * 12)
    (tmp_path / "MUTANTS.md").write_text(baseline.render([row]))
    _push_event(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mutation, "run_scoped", lambda modules, r, run, env: ([row[0]], None)
    )
    monkeypatch.setattr(mutation, "fingerprints", lambda ids, r: ({row[0]: row[4]}, {}))
    fake = ScriptedRun(
        [
            *_all_pass_prefix(),
            (_has("git", "diff"), (0, _name_status(("M", "mech/app.py")), b"")),
        ]
    )

    code = ci_push.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert capsys.readouterr().out == (
        "ci_push: verified 1 baseline row(s); mutated mech.app; 0 new survivor(s)\n"
    )


def test_ci_weekly_main_fingerprints_row_ids_against_the_root(
    tmp_path, monkeypatch, capsys
):
    """The wire between main and build_delta: row IDs (not any other cell) are
    fingerprinted against this root, and the result is what build_delta scores."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| m_one | equivalent | reason |  | aaaaaaaaaaaa |\n"
    )
    recorded = {}

    def fake_fingerprints(mutant_ids, r):
        recorded["ids"] = list(mutant_ids)
        recorded["root"] = r
        return {"m_one": "aaaaaaaaaaaa"}, {}

    monkeypatch.setattr(mutation, "fingerprints", fake_fingerprints)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "m_one: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert recorded == {"ids": ["m_one"], "root": root}
    # m_one survived with a matching fingerprint: no mismatch, no retire bucket
    assert code == 0
    assert capsys.readouterr().err == ""


def _weekly_handoff_run(results_stdout):
    return ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, results_stdout, "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )


def test_ci_weekly_files_a_handoff_for_host_divergence_alone(
    tmp_path, monkeypatch, capsys
):
    """Run 30153755743 went green and silent while holding 19 host-divergent
    rows: the bucket was computed and then dropped, because the emptiness check
    ran over an explicit key list that did not name it. A bucket nothing reports
    is a bucket that does not exist."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| m_case | environment-equivalent | GIT resolves here "
        "| reopens on a case-sensitive filesystem | aaaaaaaaaaaa |\n"
    )
    monkeypatch.setattr(
        mutation, "fingerprints", lambda ids, r: ({"m_case": "aaaaaaaaaaaa"}, {})
    )
    fake = _weekly_handoff_run("m_case: killed\n")

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0  # host-divergence alone stays green
    create = next((c for c in fake.calls if "issue" in c and "create" in c), None)
    assert create is not None, "no handoff filed for a non-empty host_divergent"
    body = create[create.index("--body") + 1]
    assert "m_case" in body


def test_ci_weekly_files_a_handoff_for_a_fingerprint_mismatch_alone(
    tmp_path, monkeypatch, capsys
):
    """Same omission, one commit earlier: a mismatch made the job red while
    filing nothing — the red badge alone is never the handoff (REQ-CI-3)."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| m_drift | equivalent | claims the old mutation |  | aaaaaaaaaaaa |\n"
    )
    monkeypatch.setattr(
        mutation, "fingerprints", lambda ids, r: ({"m_drift": "bbbbbbbbbbbb"}, {})
    )
    fake = _weekly_handoff_run("m_drift: survived\n")

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # a mismatched row is a defect
    create = next((c for c in fake.calls if "issue" in c and "create" in c), None)
    assert create is not None, "no handoff filed for a fingerprint mismatch"
    assert "m_drift" in create[create.index("--body") + 1]


def test_ci_weekly_failed_issue_listing_is_an_error_not_none_open(
    tmp_path, monkeypatch, capsys
):
    """A failed `gh issue list` was read as `raw_issues = []` — "no canonical
    issue exists" — so the run took the create path and filed a duplicate
    mech-weekly issue beside the one it could not see, exiting 0. Could-not-
    determine must be a step error (`job_red` reddens it) that files nothing:
    creating without knowing what is open IS the duplicate bug. Found by the
    2026-08-02 most-defended panel (item 9)."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| m_drift | equivalent | claims the old mutation |  | aaaaaaaaaaaa |\n"
    )
    monkeypatch.setattr(
        mutation, "fingerprints", lambda ids, r: ({"m_drift": "bbbbbbbbbbbb"}, {})
    )
    # non-empty delta (fingerprint mismatch), so the create path is armed;
    # no create/label rules scripted — an attempt raises as unscripted
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "m_drift: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (1, "", "HTTP 502")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "gh issue list" in capsys.readouterr().err
    assert not any("issue" in c and "create" in c for c in fake.calls)


def test_ci_weekly_issue_listing_exception_is_an_error_not_none_open(
    tmp_path, monkeypatch, capsys
):
    """The other arm of the same defect: the 30 s timeout (or malformed JSON)
    raised, the bare `except` swallowed it into `raw_issues = []`, and nothing
    reached `delta["errors"]` — the run could still exit 0."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| m_drift | equivalent | claims the old mutation |  | aaaaaaaaaaaa |\n"
    )
    monkeypatch.setattr(
        mutation, "fingerprints", lambda ids, r: ({"m_drift": "bbbbbbbbbbbb"}, {})
    )

    def _timeout(argv, **kw):  # noqa: ARG001  # scripted crash
        raise subprocess.TimeoutExpired(argv, 30)

    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "m_drift: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), _timeout),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert "gh issue list" in err
    assert "TimeoutExpired" in err  # the real exception class, not a rewrite
    assert not any("issue" in c and "create" in c for c in fake.calls)


def test_ci_weekly_main_appends_step_errors_to_delta(
    tmp_path, capsys
):  # REQ-CI-3 errors
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)

    def _boom_audit(argv, **kw):  # noqa: ARG001  # argv/kw unused; scripted crash
        raise OSError("pip-audit binary crashed")

    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), _boom_audit),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # job red: a step error alone is enough
    err = capsys.readouterr().err
    assert "pip-audit step errored" in err
    create_call = next(c for c in fake.calls if "issue" in c and "create" in c)
    body_arg = create_call[create_call.index("--body") + 1]
    assert "pip-audit step errored" in body_arg  # handoff still filed, error included


def test_pip_audit_uses_exported_lock_requirements(tmp_path):  # never the ambient env
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "requests==2.31.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0
    with open(os.path.join(root, ".mech", "requirements.txt"), encoding="utf-8") as fh:
        assert fh.read() == "requests==2.31.0\n"
    audit_call = next(c for c in fake.calls if "pip-audit" in c)
    assert audit_call == list(_CI["ci-pip-audit"][0])  # static argv, no ambient/-l flag


def test_ci_weekly_pip_audit_crash_exit1_empty_stdout_is_error(
    tmp_path, capsys
):  # REQ-CI-3d / E2: pip-audit 2.10.1 exits 1 both for findings AND for a
    # crash (probed: missing -r file → rc 1, empty stdout); empty stdout must
    # read as a crash, never as zero findings
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (1, "", "error: file not found")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "pip-audit failed: exit 1" in capsys.readouterr().err


def test_ci_weekly_pip_audit_unknown_exit_is_error(tmp_path, capsys):  # REQ-CI-3d / E2
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (2, json.dumps({"dependencies": []}), "usage: ...")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "pip-audit failed: exit 2" in capsys.readouterr().err


def test_ci_weekly_pip_audit_exit1_with_findings_is_findings_not_error(
    tmp_path, capsys
):  # REQ-CI-3d / E2 regression: rc 1 + JSON = findings path, not crash
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    finding = {"name": "pkg", "vulns": [{"id": "CVE-1"}]}
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (1, json.dumps({"dependencies": [finding]}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # red via the finding, not via an error
    err = capsys.readouterr().err
    assert "pip-audit failed" not in err
    create_call = next(c for c in fake.calls if "issue" in c and "create" in c)
    body_arg = create_call[create_call.index("--body") + 1]
    assert "CVE-1" in body_arg or "pkg" in body_arg


def test_ci_weekly_issue_title_date_is_utc(tmp_path):  # REQ-CI-3 handoff dating
    # UTC+14 and UTC-12 local dates are 26 h apart, so at every instant at
    # least one of them differs from the UTC date — local-time dating cannot
    # pass both iterations.
    old_tz = os.environ.get("TZ")
    try:
        for name, tz in (("tz-east", "Etc/GMT-14"), ("tz-west", "Etc/GMT+12")):
            os.environ["TZ"] = tz
            time.tzset()
            d = tmp_path / name
            d.mkdir()
            root = _weekly_repo(d)
            fake = ScriptedRun(
                [
                    (_has("mutmut", "run"), (0, "", "")),
                    (_has("mutmut", "results"), (0, "", "")),
                    (_has("uv", "export"), (0, "pkg==1.0\n", "")),
                    (_has("pip-audit"), (2, "", "boom")),  # error → issue created
                    (_has("gh", "issue", "list"), (0, "[]", "")),
                    (_has("gh", "label", "create"), (0, "", "")),
                    (_has("gh", "issue", "create"), (0, "", "")),
                ]
            )
            before = datetime.now(UTC).strftime("%Y-%m-%d")
            code = ci_weekly.main(root=root, run=fake)
            after = datetime.now(UTC).strftime("%Y-%m-%d")

            assert code == 1
            create_call = next(c for c in fake.calls if "issue" in c and "create" in c)
            title = create_call[create_call.index("--title") + 1]
            assert title in {f"mech weekly: {before}", f"mech weekly: {after}"}, tz
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_ci_weekly_mkdir_tolerates_pre_existing_mech_dir(tmp_path):  # exist_ok=True
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    (tmp_path / ".mech").mkdir()  # already present before main() runs
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "requests==2.31.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0  # must not crash with FileExistsError


def test_weekly_canonical_selection_ignores_title():  # REQ-CI-3 title-blind
    issues = [
        {
            "number": 1,
            "title": "totally unrelated",
            "labels": [{"name": "mech-weekly"}],
        },
        {
            "number": 2,
            "title": "off-label",
            "labels": [{"name": "something-else"}],
        },  # excluded: label mismatch
    ]
    assert [i["number"] for i in ci_weekly.canonical_issues(issues)] == [1]


def test_weekly_create_api_failure_exits_1(
    tmp_path, capsys
):  # API failure fails the job
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "new_survivor_1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (1, "", "API rate limited")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == "issue creation failed\n"


# --- ci_weekly.main: one exact-assertion test per branch (mutation hardening)


def _weekly_repo(tmp_path):
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(HDR)
    return root


def test_ci_weekly_mutmut_run_failure_skips_results_and_records_error(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (1, "", "boom")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "mutmut full run failed: exit 1" in capsys.readouterr().err
    assert not any(_has("mutmut", "results")(c) for c in fake.calls)


def test_ci_weekly_mutmut_run_exit2_is_accepted(
    tmp_path,
):  # mutmut's "some survived" rc
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (2, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0
    assert any(_has("mutmut", "results")(c) for c in fake.calls)


def test_ci_weekly_uses_the_serial_mutation_environment(tmp_path, monkeypatch, capsys):
    root = _weekly_repo(tmp_path)
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    mutation_kwargs = [
        kwargs
        for argv, kwargs in zip(fake.calls, fake.kwargs, strict=True)
        if _has("mutmut", "run")(argv) or _has("mutmut", "results")(argv)
    ]
    assert len(mutation_kwargs) == 2
    assert all(
        kwargs["env"]["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0"
        for kwargs in mutation_kwargs
    )


def test_ci_weekly_rejects_explicit_xdist_controls_before_mutmut(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["pkg"]\npytest_add_cli_args = ["-n4"]\n',
        encoding="utf-8",
    )
    fake = ScriptedRun(
        [
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "Pawl owns serial execution" in capsys.readouterr().err
    assert not any(_has("mutmut", "run")(call) for call in fake.calls)


def test_ci_weekly_mutmut_results_failure_records_error(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (1, "", "boom")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "mutmut results failed: exit 1" in capsys.readouterr().err


def test_ci_weekly_mutants_md_unreadable_still_files_handoff(tmp_path, capsys):
    root = _init_repo(tmp_path)  # no MUTANTS.md written
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "survivor_1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # step error alone makes the job red
    err = capsys.readouterr().err
    assert "MUTANTS.md unreadable" in err
    assert "FileNotFoundError" in err  # the real exception's class name
    assert any(
        "issue" in c and "create" in c for c in fake.calls
    )  # handoff still filed
    create = next(c for c in fake.calls if "issue" in c and "create" in c)
    body = create[create.index("--body") + 1]
    assert "## new unclassified survivors\n[]" in body
    assert "survivor_1" not in body


def test_ci_weekly_invalid_baseline_row_is_a_step_error(tmp_path, capsys):
    """`parse` is total by contract, so a hand-edited row with an unknown
    class parses fine — validate_row's verdict must reach the job, or the
    row keeps suppressing whatever mutant it names without ever being
    checked against the schema."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| mech.gate.x_f__mutmut_1 | accepted-gap | r |  | 0123456789ab |\n"
    )
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "new_survivor: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # step error alone makes the job red
    err = capsys.readouterr().err
    assert "MUTANTS.md invalid row mech.gate.x_f__mutmut_1" in err
    assert "unknown class: 'accepted-gap'" in err
    create = next(c for c in fake.calls if "issue" in c and "create" in c)
    body = create[create.index("--body") + 1]
    assert "## new unclassified survivors\n[]" in body
    assert "new_survivor" not in body


def test_ci_weekly_unparseable_results_is_an_error_not_data(tmp_path, capsys):
    """Garbled `mutmut results` output that still exits 0 must fail the run,
    not enter the delta as a catalog: reconciled against garbage, every
    baseline row read as retire-obsolete and the job stayed green — telling
    the operator to delete a correct baseline."""
    root = _init_repo(tmp_path)
    (tmp_path / "MUTANTS.md").write_text(
        HDR + "| mech.gate.x_f__mutmut_1 | equivalent | r |  | 0123456789ab |\n"
    )
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "database corrupt\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "mutmut results unparseable: 'database corrupt'" in capsys.readouterr().err
    create = next(c for c in fake.calls if "issue" in c and "create" in c)
    body = next(a for a in create if "run-id:" in a)
    # No catalog means no reconciliation: no bucket may claim one happened.
    assert "## retire-as-obsolete candidates\n[]" in body
    assert "mutmut results unparseable" in body  # the handoff carries the error


def test_ci_weekly_mutmut_step_crash_reports_exact_message(tmp_path, capsys):
    root = _weekly_repo(tmp_path)

    def _boom(argv, **kw):  # noqa: ARG001  # argv/kw unused; scripted crash
        raise OSError("mutmut binary missing")

    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), _boom),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert "mutmut step errored: OSError: mutmut binary missing" in err


def test_ci_weekly_uv_export_failure_skips_pip_audit(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (1, "", "boom")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "uv export failed: exit 1" in capsys.readouterr().err
    assert not any(_has("pip-audit")(c) for c in fake.calls)
    assert not os.path.exists(os.path.join(root, ".mech", "requirements.txt"))


def test_ci_weekly_pip_audit_malformed_json_is_step_error(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, "not json", "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    err = capsys.readouterr().err
    assert "pip-audit step errored" in err
    assert "JSONDecodeError" in err  # the real exception's class name, not "NoneType"


def test_ci_weekly_pip_audit_empty_stdout_is_error_even_at_rc0(tmp_path, capsys):
    # REQ-CI-3d / E2: supersedes the pre-E2 pin that "" fell back to "{}" —
    # `-f json` always emits a document, so empty stdout is a crash shape
    # regardless of exit code, never zero findings.
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, "", "")),  # empty stdout, rc 0
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert "pip-audit failed: exit 0" in capsys.readouterr().err


def test_ci_weekly_pip_audit_missing_dependencies_key_defaults_to_empty(
    tmp_path, capsys
):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, "{}", "")),  # valid JSON, no "dependencies" key
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err


def test_ci_weekly_gh_issue_list_crash_does_not_propagate_none(tmp_path, capsys):
    root = _weekly_repo(tmp_path)

    def _boom_list(argv, **kw):  # noqa: ARG001  # argv/kw unused; scripted crash
        raise OSError("gh missing")

    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), _boom_list),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # red on the survivor; must not crash on a gh-list failure


def test_ci_weekly_pip_audit_finds_vulnerability_is_red(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    audit_json = json.dumps(
        {"dependencies": [{"name": "bad-pkg", "vulns": [{"id": "CVE-1"}]}]}
    )
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, audit_json, "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    create_call = next(c for c in fake.calls if "issue" in c and "create" in c)
    body_arg = create_call[create_call.index("--body") + 1]
    assert "bad-pkg" in body_arg


def test_ci_weekly_delta_empty_posts_nothing_and_is_green(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    assert not any("issue" in c and "create" in c for c in fake.calls)
    assert not any("issue" in c and "comment" in c for c in fake.calls)


def test_ci_weekly_already_posted_skips_create(tmp_path, capsys, monkeypatch):
    # run-id fallback "local" is the fixture's needle; a real runner's
    # GITHUB_RUN_ID must not leak in (caught live by CI run 30036360159)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (
                _has("gh", "issue", "list"),
                (
                    0,
                    json.dumps(
                        [{"number": 1, "body": "run-id: local\n", "comments": []}]
                    ),
                    "",
                ),
            ),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1  # still red on the survivor, but no duplicate post
    assert not any("issue" in c and "create" in c for c in fake.calls)
    assert not any("issue" in c and "comment" in c for c in fake.calls)


def test_ci_weekly_one_open_issue_comments_with_its_number(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (
                _has("gh", "issue", "list"),
                (0, json.dumps([{"number": 42, "body": "old", "comments": []}]), ""),
            ),
            (_has("gh", "issue", "comment"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    comment_call = next(c for c in fake.calls if "issue" in c and "comment" in c)
    assert comment_call[comment_call.index("comment") + 1] == "42"
    # {body} placeholder is actually substituted, never left literal
    assert comment_call[comment_call.index("--body") + 1] != "{body}"
    assert "run-id: local" in comment_call[comment_call.index("--body") + 1]


def test_ci_weekly_comment_api_failure_exits_1(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (
                _has("gh", "issue", "list"),
                (0, json.dumps([{"number": 42, "body": "old", "comments": []}]), ""),
            ),
            (_has("gh", "issue", "comment"), (1, "", "rate limited")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert capsys.readouterr().err == "issue comment failed\n"


def test_ci_weekly_two_open_issues_fails_duplicates(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (
                _has("gh", "issue", "list"),
                (
                    0,
                    json.dumps(
                        [
                            {"number": 1, "body": "old", "comments": []},
                            {"number": 2, "body": "old", "comments": []},
                        ]
                    ),
                    "",
                ),
            ),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    assert (
        capsys.readouterr().err
        == "duplicate mech-weekly issues open — resolve manually\n"
    )
    assert not any("issue" in c and "comment" in c for c in fake.calls)
    assert not any("issue" in c and "create" in c for c in fake.calls)


# test_ci_weekly_gh_issue_list_failure_falls_back_to_create was deleted
# 2026-08-03: it pinned the defect itself — a failed listing taking the
# create path is the duplicate-issue bug (most-defended panel, item 9).
# Superseded by test_ci_weekly_failed_issue_listing_is_an_error_not_none_open
# and its exception-arm sibling above.


def test_ci_weekly_body_carries_the_run_id(tmp_path, monkeypatch, capsys):
    root = _weekly_repo(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1
    create_call = next(c for c in fake.calls if "issue" in c and "create" in c)
    body_arg = create_call[create_call.index("--body") + 1]
    assert "run-id: 777" in body_arg
    title_arg = create_call[create_call.index("--title") + 1]
    assert re.fullmatch(r"mech weekly: \d{4}-\d{2}-\d{2}", title_arg), title_arg


def test_ci_weekly_all_run_calls_wire_correct_kwargs(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    site_predicates = {
        "mutmut-run": (_has("mutmut", "run"), _CI["ci-mutmut-run-full"][1]),
        "mutmut-results": (_has("mutmut", "results"), tools.MUTMUT_RESULTS_TIMEOUT),
        "uv-export": (_has("uv", "export"), _CI["ci-uv-export"][1]),
        "pip-audit": (_has("pip-audit"), _CI["ci-pip-audit"][1]),
        "gh-list": (_has("gh", "issue", "list"), _CI["ci-gh-issue-list"][1]),
    }
    for name, (predicate, timeout) in site_predicates.items():
        argv, kw = next(
            (c, k) for c, k in zip(fake.calls, fake.kwargs, strict=True) if predicate(c)
        )
        assert kw["cwd"] == root, name
        assert kw["timeout"] == timeout, name
        assert kw["capture_output"] is True, name
        assert kw["text"] is True, name
        assert kw["check"] is False, name
        assert isinstance(kw["env"], dict) and kw["env"], name


def test_ci_weekly_results_argv_is_the_all_listing(tmp_path, capsys):  # REQ-CI-3a / E1
    # Plain `mutmut results` omits killed mutants, so a baselined mutant that
    # is now killed would be misfiled retire-as-obsolete instead of
    # retire-as-satisfied. The weekly flow must use the --all listing.
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    results_argv = next(c for c in fake.calls if _has("mutmut", "results")(c))
    assert results_argv == list(tools.MUTMUT_RESULTS_ALL)


def test_ci_weekly_mutmut_results_timeout_sourced_from_tools_constant(
    tmp_path, monkeypatch, capsys
):  # REQ-ARCH-9: never inlined in control flow
    monkeypatch.setattr(tools, "MUTMUT_RESULTS_TIMEOUT", 12345)
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 0, capsys.readouterr().err
    _, results_kw = next(
        (c, k)
        for c, k in zip(fake.calls, fake.kwargs, strict=True)
        if _has("mutmut", "results")(c)
    )
    assert results_kw["timeout"] == 12345


def test_ci_weekly_create_calls_wire_correct_kwargs(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (_has("gh", "issue", "list"), (0, "[]", "")),
            (_has("gh", "label", "create"), (0, "", "")),
            (_has("gh", "issue", "create"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1, capsys.readouterr().err
    for predicate, timeout in (
        (_has("gh", "label", "create"), _CI["ci-gh-label-create"][1]),
        (lambda c: "issue" in c and "create" in c, _CI["ci-gh-issue-create"][1]),
    ):
        _argv, kw = next(
            (c, k) for c, k in zip(fake.calls, fake.kwargs, strict=True) if predicate(c)
        )
        assert kw["cwd"] == root
        assert kw["timeout"] == timeout
        assert kw["capture_output"] is True
        assert kw["text"] is True
        assert kw["check"] is False
        assert isinstance(kw["env"], dict) and kw["env"]


def test_ci_weekly_comment_call_wires_correct_kwargs(tmp_path, capsys):
    root = _weekly_repo(tmp_path)
    fake = ScriptedRun(
        [
            (_has("mutmut", "run"), (0, "", "")),
            (_has("mutmut", "results"), (0, "s1: survived\n", "")),
            (_has("uv", "export"), (0, "pkg==1.0\n", "")),
            (_has("pip-audit"), (0, json.dumps({"dependencies": []}), "")),
            (
                _has("gh", "issue", "list"),
                (0, json.dumps([{"number": 42, "body": "old", "comments": []}]), ""),
            ),
            (_has("gh", "issue", "comment"), (0, "", "")),
        ]
    )

    code = ci_weekly.main(root=root, run=fake)

    assert code == 1, capsys.readouterr().err
    predicate = lambda c: "issue" in c and "comment" in c  # noqa: E731  # local one-off, not reused
    _argv, kw = next(
        (c, k) for c, k in zip(fake.calls, fake.kwargs, strict=True) if predicate(c)
    )
    assert kw["cwd"] == root
    assert kw["timeout"] == _CI["ci-gh-issue-comment"][1]
    assert kw["capture_output"] is True
    assert kw["text"] is True
    assert kw["check"] is False
    assert isinstance(kw["env"], dict) and kw["env"]


# --- B-30: the hook entry point never writes tracked files ----------------
# Wire-contract test driven via the stdin/run seams. This used to cover four
# entry points; hook_stop is the only registered one since 2026-07-25, and the
# other three modules were deleted once nothing invoked them.


def _git_status(root: str) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],  # noqa: S607  # PATH-resolved `git`; test fixture only
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_entry_points_write_no_tracked_files(  # B-30
    tmp_path_factory, capsys, monkeypatch
):
    from mech import hook_stop

    base = tmp_path_factory.mktemp("stoprepo")
    tracked = base / "tracked.py"
    tracked.write_text("a = 1\n")
    root = _init_repo(base)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)  # noqa: S607  # PATH-resolved `git`; test fixture only
    before = _git_status(root)

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})),
    )
    hook_stop.main(
        run=lambda *a, **k: subprocess.CompletedProcess(  # noqa: ARG005  # k unused
            a, 0, stdout="1 passed", stderr=""
        )
    )
    capsys.readouterr()

    assert _git_status(root) == before


# --- REQ-CI-3 delta bucketing property ---------------------------------------
#
# The examples above pin each bucket one at a time. The property they cannot
# state is the one that makes the weekly handoff trustworthy: across ANY
# combination of run states, baseline classes and fingerprints, every baseline
# row is settled into exactly one bucket — never two (the double-report the
# `in generated` guard was added for), never none.
_DELTA_IDS = ("m0", "m1", "m2", "m3")
_DELTA_FPS = ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
# Detected/survivor/untested states as pinned in mutation.py's wire contract.
_DELTA_STATES = ("killed", "timeout", "survived", "no tests")

_delta_rows = st.dictionaries(
    st.sampled_from(_DELTA_IDS),
    st.tuples(st.sampled_from(baseline.CLASSES), st.sampled_from(_DELTA_FPS)),
)
_delta_states = st.dictionaries(
    st.sampled_from(_DELTA_STATES),
    # "fresh" is a mutant mutmut generated that no baseline row mentions.
    st.lists(st.sampled_from((*_DELTA_IDS, "fresh"))),
)


@given(
    _delta_rows,
    _delta_states,
    st.dictionaries(st.sampled_from(_DELTA_IDS), st.sampled_from(_DELTA_FPS)),
)
def test_build_delta_settles_each_baseline_row_into_exactly_one_bucket(
    row_map, full_states, actual_fingerprints
):
    rows = [(mid, cls, "reason", "", fp) for mid, (cls, fp) in row_map.items()]
    delta = ci_weekly.build_delta(full_states, rows, actual_fingerprints)

    baseline_ids = set(row_map)
    generated = {mid for ids in full_states.values() for mid in ids}
    survived = {
        mid for state in mutation.SURVIVOR_STATES for mid in full_states.get(state, [])
    }
    detected = {
        mid for state in mutation.PASSING_STATES for mid in full_states.get(state, [])
    }

    settled = (
        delta["retire_satisfied"]
        + delta["host_divergent"]
        + delta["retire_obsolete"]
        + delta["inconclusive"]
    )
    assert len(settled) == len(set(settled))  # no row reported under two buckets
    assert set(settled) <= baseline_ids
    # The rows left unsettled are exactly the ones still doing their job:
    # generated by this run and still surviving it.
    assert baseline_ids - set(settled) == {
        mid for mid in baseline_ids if mid in generated and mid in survived
    }
    # Only a detected state (killed/timeout) is a kill; anything else generated
    # and not surviving proves nothing about the row and must say so.
    assert set(delta["retire_satisfied"]) | set(delta["host_divergent"]) <= detected
    assert set(delta["inconclusive"]) == {
        mid
        for mid in baseline_ids
        if mid in generated and mid not in survived and mid not in detected
    }
    # A survivor is "new" iff no baseline row already accounts for it.
    assert set(delta["new_survivors"]) == survived - baseline_ids
    # An ungenerated row has no fingerprint to compare, so it is retire-obsolete
    # and nothing else.
    assert set(delta["fingerprint_mismatch"]).isdisjoint(delta["retire_obsolete"])
    for bucket in (
        "new_survivors",
        "retire_satisfied",
        "host_divergent",
        "retire_obsolete",
        "inconclusive",
        "fingerprint_mismatch",
    ):
        assert delta[bucket] == sorted(set(delta[bucket]))


_WORKFLOWS = os.path.join(paths.repo_root(os.getcwd()) or ".", ".github", "workflows")


# `mech.validation` deliberately treats an absent `.github/workflows/` as
# "nothing to check yet" rather than an error, so a deployment may decline CI
# and still validate. Without this guard the bare `os.listdir` below raised
# FileNotFoundError in exactly that deployment — turning a supported choice into
# a red commit gate, since the gate runs this suite. Measured 2026-07-29 on a
# brownfield adoption that declined CI. Pawl itself always has workflows, so the
# guard never fires here and cannot mask rot.
@pytest.mark.skipif(
    not os.path.isdir(_WORKFLOWS),
    reason="no .github/workflows/ — this deployment declined CI",
)
def test_workflows_validate_the_lockfile_rather_than_trusting_it():
    """`uv sync --frozen` installs from uv.lock WITHOUT checking that the lock
    still matches pyproject.toml. Measured 2026-07-28 on a repo whose
    pyproject declared a dependency absent from the lock: --frozen exited 0 and
    simply never installed it, so CI would have gone green while testing a
    dependency set nobody declared. --locked exited 1 naming the stale lock.

    That drift is reachable: the pre-commit hook's own `uv run` re-locks, so
    committing a staged subset that includes pyproject.toml but not the
    regenerated uv.lock produces exactly this state.
    """
    workflows = os.path.join(
        paths.repo_root(os.getcwd()) or ".", ".github", "workflows"
    )
    checked = 0
    for name in sorted(os.listdir(workflows)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(workflows, name), encoding="utf-8") as fh:
            text = fh.read()
        assert "uv sync --frozen" not in text, f"{name} trusts the lock unchecked"
        if "uv sync" in text:
            assert "uv sync --locked" in text, f"{name} syncs without --locked"
            checked += 1
    assert checked, "no workflow ran `uv sync` — has the CI entry point moved?"
