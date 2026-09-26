import subprocess
import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from mech import mutation, source_inventory, tools

FIXTURE = (Path(__file__).parent / "fixtures" / "mutmut_results.txt").read_text()


def test_module_for_rule():  # B-18
    inventory = source_inventory.SourceInventory(
        source_paths=("mech",), do_not_mutate=("mech/tests/*",)
    )
    assert inventory.module_for("mech/gate.py") == "mech.gate"
    assert inventory.module_for("mech/__init__.py") == "mech"
    assert inventory.module_for("docs/readme.md") is None
    assert inventory.module_for("mech/tests/test_x.py") is None


def test_parse_results_fixture_yields_states():
    states = mutation.parse_results(FIXTURE)
    assert isinstance(states, dict)
    assert all(isinstance(v, list) for v in states.values())
    # Concrete state + concrete mutant ID from the real captured fixture, not
    # just an isinstance check that would also pass for {}.
    assert "survived" in states
    assert states["survived"][0] == "mech.paths.x_repo_root__mutmut_6"


ALL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "mutmut_results_all.txt"
).read_text()


def test_parse_results_all_listing_includes_killed():  # REQ-CI-3a / E1
    # Real capture of `mutmut results --all true` (mutmut 3.6, replayed on 3.7):
    # the --all
    # listing is the only one that shows killed mutants, which the weekly
    # retire classification needs to file killed rows as retire-satisfied.
    states = mutation.parse_results(ALL_FIXTURE)
    assert states["killed"] == [
        "mech.verdict.x_finding__mutmut_1",
        "mech.verdict.x_finding__mutmut_2",
        "mech.verdict.x_finding__mutmut_3",
        "mech.verdict.x_finding__mutmut_4",
    ]
    assert "not checked" in states  # --all vocabulary, pinned from real capture


def test_classify_survivors_and_errors():  # B-35
    ok = mutation.classify({"killed": ["a"], "survived": ["b", "c"]})
    assert ok == {"survivors": ["b", "c"], "error": None}
    # timeout = detection: hung suite means the mutant changed behavior; the
    # timeout set is also machine-load-dependent (REQ-CG-4 amendment 2026-07-23)
    ok_timeout = mutation.classify({"killed": ["a"], "timeout": ["z"]})
    assert ok_timeout == {"survivors": [], "error": None}
    bad = mutation.classify({"killed": ["a"], "suspicious": ["z"]})
    assert bad["error"] is not None and "suspicious" in bad["error"]


def test_classify_zero_mutants_passes():  # B-35
    assert mutation.classify({}) == {"survivors": [], "error": None}


def test_classify_skips_empty_ids_without_stopping_iteration():
    # A leading state entry with an empty ID list must be skipped (`continue`)
    # not treated as a stop condition (`break`) — a later, real-content state
    # in the same dict must still be reached.
    outcome = mutation.classify({"skipped_empty": [], "survived": ["a"]})
    assert outcome == {"survivors": ["a"], "error": None}


def test_parse_results_garbage_truncates_to_exactly_200_chars():
    garbage = "x" * 250 + "\nmore garbage\n"
    states = mutation.parse_results(garbage)
    assert len(states[mutation.UNPARSEABLE_STATE][0]) == 200


def test_parse_results_garbage_is_error_not_empty():  # B-35
    # Non-empty text matching no result-record pattern must never read as a
    # valid (empty) zero-mutant catalog.
    outcome = mutation.classify(
        mutation.parse_results("not mutmut output at all\ngarbage\n")
    )
    assert outcome["error"] is not None
    assert "unparseable" in outcome["error"].lower()


def test_parse_results_surfaces_malformed_records_beside_parsed_ones():
    """One good line must not buy silence for the rest: with malformed
    records dropped, a truncated or partly-written results stream reads as
    a complete, clean verdict."""
    states = mutation.parse_results(
        "mech.a.x_f__mutmut_1: killed\nmech.a.x_g__mutmut_2 ??? survived\n"
    )
    assert states["killed"] == ["mech.a.x_f__mutmut_1"]
    assert states[mutation.UNPARSEABLE_STATE] == ["mech.a.x_g__mutmut_2 ??? survived"]


def test_parse_results_lists_every_unreadable_line():
    states = mutation.parse_results("not mutmut output at all\ngarbage\n")
    assert states[mutation.UNPARSEABLE_STATE] == [
        "not mutmut output at all",
        "garbage",
    ]


def test_parse_results_continues_after_blank_lines():
    states = mutation.parse_results(
        "mech.a.x_first__mutmut_1: killed\n\nmech.a.x_second__mutmut_2: survived\n"
    )
    assert states == {
        "killed": ["mech.a.x_first__mutmut_1"],
        "survived": ["mech.a.x_second__mutmut_2"],
    }


def _completed(argv, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def test_invocation_error_preserves_both_streams_and_line_shape():
    proc = _completed(
        ["mutmut"],
        1,
        stdout="  stdout detail   \n",
        stderr="  stderr detail   \n",
    )
    assert mutation._invocation_error("failed", proc) == (
        "failed\n  stdout detail\n  stderr detail"
    )


def _scripted(bodies, rc_after_first=None):
    """fake_run yielding a different `mutmut results` stdout per attempt.

    `bodies[i]` is the results stdout for attempt i; the last entry repeats.
    `rc_after_first` makes every `mutmut run` after the first exit with that
    code, for testing failure on a retry.
    """
    state = {"runs": 0, "results": 0}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            state["runs"] += 1
            if rc_after_first is not None and state["runs"] > 1:
                return _completed(argv, rc_after_first)
            return _completed(argv, 0)
        body = bodies[min(state["results"], len(bodies) - 1)]
        state["results"] += 1
        return _completed(argv, 0, stdout=body)

    fake_run.state = state  # pyright: ignore[reportFunctionMemberAccess]
    return fake_run


def test_a_crash_state_is_retried_once_and_the_retry_result_wins():
    # Replace, never merge: the pre-retry segfault must not survive into the
    # verdict, or one mutant would hold two states from a single collection.
    fake_run = _scripted(
        [
            "    mech.paths.x_a__mutmut_1: segfault\n",
            "    mech.paths.x_a__mutmut_1: survived\n",
        ]
    )
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert error is None
    assert survivors == ["mech.paths.x_a__mutmut_1"]
    assert fake_run.state["runs"] == 2  # pyright: ignore[reportFunctionMemberAccess]


def test_a_module_with_both_a_crash_state_and_survivors_is_retried_whole():
    # REQ-MUT-2 retries the module, not the crash-state subset. Without a
    # survivor in the first body, a "retry only when nothing survived" bug
    # and a merge bug both stay green.
    fake_run = _scripted(
        [
            "    mech.paths.x_a__mutmut_1: survived\n"
            "    mech.paths.x_b__mutmut_1: segfault\n",
            "    mech.paths.x_b__mutmut_1: survived\n",
        ]
    )
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert error is None
    # Only the retry's body counts: x_a came from the discarded pre-retry map.
    assert survivors == ["mech.paths.x_b__mutmut_1"]
    assert fake_run.state["runs"] == 2  # pyright: ignore[reportFunctionMemberAccess]


def test_run_scoped_fail_closes_when_a_crash_state_persists_after_one_retry():
    fake_run = _scripted(["    mech.paths.x_a__mutmut_1: segfault\n"])
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error is not None and "segfault" in error
    # one attempt plus one retry, never a loop
    assert fake_run.state["runs"] == 2  # pyright: ignore[reportFunctionMemberAccess]


def test_a_skipped_state_does_not_trigger_a_retry():
    # mutmut renders a deliberate `# pragma: no mutate` as `skipped`. Retrying
    # it would make every module holding one intentional pragma retry and then
    # fail closed, forever.
    fake_run = _scripted(["    mech.paths.x_a__mutmut_1: skipped\n"])
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert fake_run.state["runs"] == 1  # pyright: ignore[reportFunctionMemberAccess]
    assert survivors == []
    assert error is not None and "skipped" in error


def test_unparseable_results_are_not_retried():
    # It is the adapter's own signal, not a mutmut state. Retrying could let a
    # garbled blob read as a clean pass on the second attempt.
    fake_run = _scripted(["not mutmut output at all\n"])
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert fake_run.state["runs"] == 1  # pyright: ignore[reportFunctionMemberAccess]
    assert survivors == []
    assert error is not None and "unparseable" in error.lower()


def test_a_failure_on_the_retry_does_not_resurrect_the_pre_retry_states():
    fake_run = _scripted(["    mech.paths.x_a__mutmut_1: segfault\n"], rc_after_first=7)
    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error == "mutmut run failed for mech.paths: exit 7"


def test_the_retry_line_sorts_and_deduplicates_multiple_crash_states(capsys):
    # Sorted so the line is not a function of dict insertion order, which is
    # parse order and therefore mutmut's.
    fake_run = _scripted(
        [
            "    mech.paths.x_a__mutmut_1: segfault\n"
            "    mech.paths.x_b__mutmut_1: caught by type check\n"
            "    mech.paths.x_c__mutmut_1: segfault\n",
            "    mech.paths.x_a__mutmut_1: survived\n",
        ]
    )
    mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert (
        "retrying mech.paths after transient state(s): "
        "caught by type check, segfault" in capsys.readouterr().err
    )


def test_a_second_diagnostic_line_appears_only_when_the_crash_state_persists(capsys):
    mutation.run_scoped(
        ["mech.paths"],
        "/repo",
        run=_scripted(["    mech.paths.x_a__mutmut_1: segfault\n"]),
    )
    persistent = [
        ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("INFO:")
    ]
    mutation.run_scoped(
        ["mech.paths"],
        "/repo",
        run=_scripted(
            [
                "    mech.paths.x_a__mutmut_1: segfault\n",
                "    mech.paths.x_a__mutmut_1: survived\n",
            ]
        ),
    )
    cleared = [
        ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("INFO:")
    ]
    assert len(persistent) == 2
    assert len(cleared) == 1
    assert persistent[-1] == (
        "INFO: mech.paths still reports a transient state after one retry; "
        "a repeating crash is more likely memory (SIGKILL from the OOM killer) "
        "than noise"
    )


def test_run_scoped_filters_survivors_to_requested_modules():  # B-18 substrate
    results_text = (
        "    mech.paths.x_repo_root__mutmut_1: survived\n"
        "    mech.bash_guard.x_tokenize__mutmut_2: survived\n"
    )

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == ["mech.paths.x_repo_root__mutmut_1"]
    assert error is None


def test_package_module_scope_does_not_widen_to_every_submodule():
    results_text = (
        "    mech.x_root__mutmut_1: survived\n"
        "    mech.xǁRootǁmethod__mutmut_2: survived\n"
        "    mech.child.x_other__mutmut_3: no tests\n"
        "    mech.xylophone.x_other__mutmut_4: no tests\n"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(["mech"], "/repo", run=fake_run)

    assert error is None
    assert survivors == [
        "mech.x_root__mutmut_1",
        "mech.xǁRootǁmethod__mutmut_2",
    ]
    assert calls[0][-1] == "mech.x[_ǁ]*"


def test_x_prefixed_existing_module_is_not_mistaken_for_a_function(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "x_api.py").write_text("def handle():\n    return 1\n")
    results_text = "pkg.x_api.x_handle__mutmut_1: survived\n"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(["pkg.x_api"], str(tmp_path), run=fake_run)

    assert error is None
    assert survivors == ["pkg.x_api.x_handle__mutmut_1"]
    assert calls[0][-1] == "pkg.x_api.x[_ǁ]*"


def test_x_prefixed_existing_package_is_not_mistaken_for_a_function(tmp_path):
    package = tmp_path / "pkg" / "x_api"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def handle():\n    return 1\n")
    results_text = "pkg.x_api.x_handle__mutmut_1: survived\n"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(["pkg.x_api"], str(tmp_path), run=fake_run)

    assert error is None
    assert survivors == ["pkg.x_api.x_handle__mutmut_1"]
    assert calls[0][-1] == "pkg.x_api.x[_ǁ]*"


def test_run_scoped_keeps_survivors_when_scoped_to_a_function():
    # Mutant IDs are "<module>.<function>__mutmut_<n>", so a function-scoped
    # filter is followed by "__mutmut_", never ".". mutmut matches its own
    # "<name>*" filter by fnmatch and duly runs those mutants — so the result
    # filter here must not drop them and report a confident empty catalog.
    # Every MUTANTS.md reopen condition that says `mutmut run
    # "mech.gate.x__staged_files*"` is scoped exactly this way.
    results_text = (
        "    mech.gate.x__staged_files__mutmut_1: survived\n"
        "    mech.gate.x_other_function__mutmut_2: survived\n"
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(
        ["mech.gate.x__staged_files"], "/repo", run=fake_run
    )
    # The sibling function's mutant must still be excluded — the fix must not
    # widen the filter into "any ID sharing a prefix".
    assert survivors == ["mech.gate.x__staged_files__mutmut_1"]
    assert error is None
    assert calls[0][-1] == "mech.gate.x__staged_files*"


def test_run_scoped_keeps_class_method_survivors_when_scoped_to_a_function():
    results_text = "mech.gate.xǁGateǁrun__mutmut_1: survived\n"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(
        ["mech.gate.xǁGateǁrun"], "/repo", run=fake_run
    )

    assert error is None
    assert survivors == ["mech.gate.xǁGateǁrun__mutmut_1"]
    assert calls[0][-1] == "mech.gate.xǁGateǁrun*"


def test_run_scoped_bad_returncode_fails_closed():  # B-20 substrate
    def fake_run(argv, **kwargs):
        return _completed(argv, 1, stderr="boom")

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error is not None and "mutmut" in error


def test_run_scoped_threads_env_to_both_subprocess_calls():
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs.get("env"))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="")

    sentinel_env = {"FOO": "bar"}
    mutation.run_scoped(["mech.paths"], "/repo", run=fake_run, env=sentinel_env)
    assert recorded == [sentinel_env, sentinel_env]


def test_run_scoped_default_env_is_the_isolated_mutation_context(
    monkeypatch,
):
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs.get("env"))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="")

    mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert len(recorded) == 2
    assert all(env["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0" for env in recorded)


def test_survey_default_env_is_the_isolated_mutation_context(monkeypatch):
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs.get("env"))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="")

    mutation.survey(["mech.paths"], "/repo", run=fake_run)
    assert len(recorded) == 2
    assert all(env["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0" for env in recorded)


def test_regenerate_tree_default_env_is_the_isolated_mutation_context(monkeypatch):
    monkeypatch.setattr(mutation, "_xdist_installed", lambda: True)
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return _completed(argv, 0)

    assert mutation.regenerate_tree("/repo", run=fake_run) is None
    assert recorded["env"]["PYTEST_ADDOPTS"] == "-p xdist.plugin -n0"


def test_run_scoped_zero_mutants_signature_passes_with_info_line(capsys):
    # REQ-CG-4 zero-mutants pass-through: the captured real mutmut crash
    # signature for a pure-data module (task-12-report.md dogfood run).
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return _completed(
                argv,
                1,
                stderr=(
                    "AssertionError: Filtered for specific mutants, but "
                    "nothing matches\n\nFilter: ('mech.tools*',)\n"
                ),
            )
        return _completed(argv, 0, stdout="")

    survivors, error = mutation.run_scoped(["mech.tools"], "/repo", run=fake_run)
    assert survivors == []
    assert error is None
    assert "INFO: no mutants generated for mech.tools" in capsys.readouterr().err


def test_run_scoped_other_run_failure_stays_fail_closed():
    # A different nonzero-exit crash (not the zero-mutants signature) must
    # still fail-closed exactly as before — the pass-through is narrow.
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return _completed(argv, 1, stderr="Traceback: something else broke\n")
        return _completed(argv, 0, stdout="")

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error is not None
    assert error.startswith("mutmut run failed for mech.paths: exit 1")


def test_run_scoped_fails_closed_when_the_results_call_fails():
    """`mutmut run` succeeding and `mutmut results` failing is its own path:
    without it, a nonzero results exit would fall through to parse_results on
    empty stdout and read as a clean zero-survivor pass."""

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 3)

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error == "mutmut results failed for mech.paths: exit 3"


def test_run_scoped_names_the_module_when_the_results_call_fails():
    # The taxonomy names the module in every row, so a multi-module run says
    # which one broke instead of leaving the operator to guess. The `results`
    # argv carries no module, so the fake remembers the one the preceding
    # `run` asked for.
    seen = {"module": ""}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            seen["module"] = argv[4]
            return _completed(argv, 0)
        if seen["module"].startswith("mech.gate"):
            return _completed(argv, 3)
        return _completed(argv, 0, stdout="")

    survivors, error = mutation.run_scoped(
        ["mech.paths", "mech.gate"], "/repo", run=fake_run
    )
    assert survivors == []
    assert error == "mutmut results failed for mech.gate: exit 3"


def test_run_scoped_reports_a_run_timeout_distinctly_from_a_results_timeout():
    # TimeoutExpired currently falls into the blanket `except Exception` and
    # surfaces as an opaque adapter error naming no phase.
    def timing_out_on(which):
        def fake_run(argv, **kwargs):
            if argv[3] == which:
                raise subprocess.TimeoutExpired(argv, 600)
            return _completed(argv, 0, stdout="")

        return fake_run

    _, run_err = mutation.run_scoped(["mech.paths"], "/repo", run=timing_out_on("run"))
    _, res_err = mutation.run_scoped(
        ["mech.paths"], "/repo", run=timing_out_on("results")
    )
    assert run_err == "mutmut run timed out for mech.paths after 600s"
    assert res_err == "mutmut results timed out for mech.paths after 600s"


def test_run_scoped_run_call_full_kwargs_exact():
    # Pins every subprocess.run kwarg for BOTH calls (the "mutmut run" call
    # and the "mutmut results" call) — cwd/capture_output/text/timeout/
    # check/env — so a mutant dropping or swapping any single kwarg on
    # either call is caught.
    recorded_calls = []

    def fake_run(argv, **kwargs):
        recorded_calls.append(dict(kwargs))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="")

    sentinel_env = {"X": "1"}
    mutation.run_scoped(
        ["mech.paths"], "/repo", run=fake_run, timeout=42, env=sentinel_env
    )
    assert len(recorded_calls) == 2
    for kwargs in recorded_calls:
        assert kwargs == {
            "cwd": "/repo",
            "capture_output": True,
            "text": True,
            "timeout": 42,
            "check": False,
            "env": sentinel_env,
        }


def test_run_scoped_retry_preserves_root_timeout_and_environment():
    recorded_calls = []
    results = iter(
        [
            "mech.paths.x_a__mutmut_1: segfault\n",
            "mech.paths.x_a__mutmut_1: survived\n",
        ]
    )

    def fake_run(argv, **kwargs):
        recorded_calls.append((list(argv), dict(kwargs)))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout=next(results))

    sentinel_env = {"MUTMUT_TEST_TIME_MULTIPLIER": "2"}
    survivors, error = mutation.run_scoped(
        ["mech.paths"], "/repo", run=fake_run, timeout=42, env=sentinel_env
    )

    assert error is None
    assert survivors == ["mech.paths.x_a__mutmut_1"]
    assert len(recorded_calls) == 4
    for _, kwargs in recorded_calls:
        assert kwargs["cwd"] == "/repo"
        assert kwargs["timeout"] == 42
        assert kwargs["env"] is sentinel_env


def test_run_scoped_default_timeout_is_600_on_both_calls():
    recorded = []

    def fake_run(argv, **kwargs):
        recorded.append(kwargs.get("timeout"))
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="")

    mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert recorded == [600, 600]


def test_run_scoped_rejects_explicit_xdist_config_before_launch(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["mech"]\npytest_add_cli_args = ["-n4"]\n'
    )

    def should_not_run(argv, **kwargs):
        raise AssertionError("conflicting configuration must block before mutmut")

    survivors, error = mutation.run_scoped(
        ["mech.paths"], str(tmp_path), run=should_not_run
    )

    assert survivors == []
    assert error is not None and "Pawl owns serial execution" in error


def test_run_scoped_adapter_error_names_real_exception_type():
    def fake_run(argv, **kwargs):
        raise ValueError("boom")

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error == "mutmut adapter error: ValueError: boom"


def test_run_scoped_classification_error_names_real_exception(monkeypatch):
    def explode(_states):
        raise ValueError("classification boom")

    monkeypatch.setattr(mutation, "classify", explode)
    survivors, error = mutation.run_scoped([], "/repo")
    assert survivors == []
    assert error == "mutmut adapter error: ValueError: classification boom"


def test_run_scoped_zero_mutants_continues_to_next_module(capsys):
    # The pass-through must `continue` the per-module loop, not abort it:
    # a later module in the same call still gets run and its survivors
    # still surface.
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            if argv[-1] == "mech.tools.x[_ǁ]*":
                return _completed(
                    argv,
                    1,
                    stderr=(
                        "AssertionError: Filtered for specific mutants, but "
                        "nothing matches\n\nFilter: ('mech.tools*',)\n"
                    ),
                )
            return _completed(argv, 0)
        return _completed(
            argv, 0, stdout="mech.paths.x_repo_root__mutmut_1: survived\n"
        )

    survivors, error = mutation.run_scoped(
        ["mech.tools", "mech.paths"], "/repo", run=fake_run
    )
    assert error is None
    assert survivors == ["mech.paths.x_repo_root__mutmut_1"]
    assert "INFO: no mutants generated for mech.tools" in capsys.readouterr().err


def test_run_scoped_garbage_results_fail_closed():  # Task 11 review, REQ-CG-4/CG-5
    # mutmut exits 0 for both steps, but `results` stdout matches no
    # "<id>: <state>" record pattern at all. The garbage pseudo-ID (first
    # line of the blob) matches no requested module prefix, so a filter
    # applied before classify must not silently drop it to an empty,
    # passing catalog — it must still reach classify and fail closed.
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return _completed(argv, 0)
        return _completed(
            argv, 0, stdout="totally garbled nonsense not matching any pattern\n"
        )

    survivors, error = mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert survivors == []
    assert error is not None
    assert "unparseable" in error.lower()


def test_run_scoped_fetches_results_per_module_before_next_run():  # Task 12 regression
    """Verify that results are fetched INSIDE the per-module loop.

    Discriminating regression test: ensures the fix (fetch results after each
    module's run) is maintained. Without it, reverting to fetch-once-after-loop
    would still pass existing tests because they use static results.

    Setup: Simulates mutmut 3.7.0 behavior where each `mutmut run` resets
    previously-recorded results. The `results` output only contains entries
    for the module whose `run` was most recently called.

    Modules:
    - mech.a: Has a survivor `mech.a.x_f__mutmut_1`
    - mech.b: Has only killed mutants (no entries in results by default)

    CORRECT flow (results fetched per-module, in-loop):
    1. Loop iteration 1 (mech.a):
       a. mutmut run `mech.a.x[_ǁ]*` → returns 0
       b. mutmut results → returns "mech.a.x_f__mutmut_1: survived"
       c. Parse and accumulate: survivor captured
    2. Loop iteration 2 (mech.b):
       a. mutmut run `mech.b.x[_ǁ]*` → returns 0 (this "resets" results)
       b. mutmut results → returns "" (empty, only killed mutants)
       c. Parse and accumulate: nothing new
    3. Return: ["mech.a.x_f__mutmut_1"]

    BUGGY flow (results fetched once, after loop):
    1. Loop iteration 1 (mech.a):
       a. mutmut run `mech.a.x[_ǁ]*` → returns 0
    2. Loop iteration 2 (mech.b):
       a. mutmut run `mech.b.x[_ǁ]*` → returns 0 (this "resets" results)
    3. mutmut results (ONCE after loop) → returns "" (only mech.b data)
    4. Return: [] (survivor LOST)

    Assertion: The survivor IS returned, proving per-module fetching is active.
    """
    # Track the most recently-run module to simulate mutmut's per-run reset
    last_run: dict[str, str | None] = {"module": None}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            # mutmut run call
            filter_arg = argv[-1]
            if filter_arg == "mech.a.x[_ǁ]*":
                last_run["module"] = "mech.a"
            elif filter_arg == "mech.b.x[_ǁ]*":
                last_run["module"] = "mech.b"
            return _completed(argv, 0)

        # mutmut results call
        # Return results only for the module that was most recently run
        # (simulating the reset that happens on each `mutmut run` invocation)
        if last_run["module"] == "mech.a":
            results_text = "mech.a.x_f__mutmut_1: survived\n"
        elif last_run["module"] == "mech.b":
            # mech.b has no survivors (only killed mutants, omitted by default)
            results_text = ""
        else:
            results_text = ""

        return _completed(argv, 0, stdout=results_text)

    survivors, error = mutation.run_scoped(["mech.a", "mech.b"], "/repo", run=fake_run)
    assert error is None
    # The survivor from mech.a should be captured because results were fetched
    # immediately after mech.a's run (line 139 in mutation.py), before mech.b's
    # run could reset the results (line 118 would have been the buggy location).
    assert survivors == ["mech.a.x_f__mutmut_1"]


# --- mutation.survey: the unjudged operator read -----------------------------


def test_survey_returns_the_raw_map_scoped_to_the_requested_modules():
    # B1 has two halves and this proves both. Raw: `classify` fail-closes on
    # `no tests`, discarding the survivor beside it, and that is the exact
    # shape a brownfield module starts from. Scoped: mutmut re-collects across
    # the whole tree, so a results body legitimately carries IDs the caller
    # never asked for.
    fake_run = _scripted(
        [
            "    mech.paths.x_a__mutmut_1: survived\n"
            "    mech.paths.x_b__mutmut_1: no tests\n"
            "    mech.gate.x_c__mutmut_1: survived\n"
        ]
    )
    states, error = mutation.survey(["mech.paths"], "/repo", run=fake_run)
    assert error is None
    assert states == {
        "survived": ["mech.paths.x_a__mutmut_1"],
        "no tests": ["mech.paths.x_b__mutmut_1"],
    }


def test_survey_with_an_empty_module_list_returns_an_empty_map():
    def fake_run(argv, **kwargs):
        raise AssertionError("must not invoke mutmut for zero modules")

    assert mutation.survey([], "/repo", run=fake_run) == ({}, None)


def test_survey_passes_a_zero_mutant_module_through_and_keeps_going(capsys):
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            if "mech.tools" in argv[4]:
                return _completed(argv, 1, stderr=mutation.ZERO_MUTANTS_SIGNATURE)
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.tools", "mech.paths"], "/repo", run=fake_run)
    assert error is None
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert "no mutants generated for mech.tools" in capsys.readouterr().err


def test_survey_keeps_earlier_states_when_a_later_module_fails():
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            return (
                _completed(argv, 0) if "mech.paths" in argv[4] else _completed(argv, 9)
            )
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut run failed for mech.gate: exit 9"


def test_survey_keeps_earlier_states_when_a_later_module_times_out():
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            if "mech.gate" in argv[4]:
                raise subprocess.TimeoutExpired(argv, 600)
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut run timed out for mech.gate after 600s"


def test_survey_keeps_partial_states_on_a_generic_adapter_exception():
    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            if "mech.gate" in argv[4]:
                raise RuntimeError("boom")
            return _completed(argv, 0)
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut adapter error: RuntimeError: boom"


def test_survey_and_run_scoped_diverge_on_the_same_failure():
    # The architecture in one test: a gate must not act on partial data, a
    # diagnostic tool must not discard work the operator paid for.
    def make_run():
        def fake_run(argv, **kwargs):
            if argv[3] == "run":
                return (
                    _completed(argv, 0)
                    if "mech.paths" in argv[4]
                    else _completed(argv, 9)
                )
            return _completed(
                argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n"
            )

        return fake_run

    states, survey_err = mutation.survey(
        ["mech.paths", "mech.gate"], "/repo", run=make_run()
    )
    survivors, scoped_err = mutation.run_scoped(
        ["mech.paths", "mech.gate"], "/repo", run=make_run()
    )
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert survivors == []
    assert survey_err == scoped_err == "mutmut run failed for mech.gate: exit 9"


def test_survey_run_failure_carries_the_captured_diagnostic():
    """`survey` is the operator's entry point, so its failures are the ones a
    human reads. A bare exit code makes a poisoned `mutants/` tree — mutmut's
    most common failure, and one the repo's own green suite does not explain —
    indistinguishable from a broken mutmut."""

    def fake_run(argv, **kwargs):
        return _completed(
            argv, 1, stdout="ModuleNotFoundError: No module named 'scripts'\n"
        )

    _, error = mutation.survey(["mech.gate"], "/repo", run=fake_run)
    assert error is not None
    assert error.startswith("mutmut run failed for mech.gate: exit 1")
    assert "No module named 'scripts'" in error


def test_survey_reports_a_results_failure_with_the_states_already_collected():
    # The `mutmut results` argv carries no module, so the fake remembers which
    # module the preceding `mutmut run` asked for. Every per-module fake below
    # does the same.
    seen = {"module": ""}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            seen["module"] = argv[4]
            return _completed(argv, 0)
        if seen["module"].startswith("mech.gate"):
            return _completed(argv, 4)
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut results failed for mech.gate: exit 4"


def test_survey_reports_a_results_timeout_with_the_states_already_collected():
    seen = {"module": ""}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            seen["module"] = argv[4]
            return _completed(argv, 0)
        if seen["module"].startswith("mech.gate"):
            raise subprocess.TimeoutExpired(argv, 600)
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut results timed out for mech.gate after 600s"


def test_survey_does_not_resurrect_pre_retry_states_when_the_retry_fails():
    # B10's survey half. The earlier successful module is what makes
    # resurrection visible: if the discarded segfault map came back it would
    # appear in `states` beside mech.paths, and the assertion below would fail.
    seen = {"module": "", "gate_runs": 0}

    def fake_run(argv, **kwargs):
        if argv[3] == "run":
            seen["module"] = argv[4]
            if seen["module"].startswith("mech.gate"):
                seen["gate_runs"] += 1
                if seen["gate_runs"] > 1:  # the retry
                    return _completed(argv, 7)
            return _completed(argv, 0)
        if seen["module"].startswith("mech.gate"):
            return _completed(argv, 0, stdout="    mech.gate.x_b__mutmut_1: segfault\n")
        return _completed(argv, 0, stdout="    mech.paths.x_a__mutmut_1: survived\n")

    states, error = mutation.survey(["mech.paths", "mech.gate"], "/repo", run=fake_run)
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert error == "mutmut run failed for mech.gate: exit 7"
    assert seen["gate_runs"] == 2


def test_survey_surfaces_unparseable_state_in_the_map_not_the_error():
    states, error = mutation.survey(
        ["mech.paths"], "/repo", run=_scripted(["not mutmut output at all\n"])
    )
    assert error is None
    assert mutation.UNPARSEABLE_STATE in states


def test_the_mutmut_timeout_state_is_data_not_an_adapter_timeout():
    # `timeout` is a mutmut result state (a mutant that hung, in
    # PASSING_STATES) and must not be confused with subprocess TimeoutExpired.
    states, error = mutation.survey(
        ["mech.paths"],
        "/repo",
        run=_scripted(["    mech.paths.x_a__mutmut_1: timeout\n"]),
    )
    assert error is None
    assert states == {"timeout": ["mech.paths.x_a__mutmut_1"]}


def test_survey_retries_a_crash_state_once_like_run_scoped():
    fake_run = _scripted(
        [
            "    mech.paths.x_a__mutmut_1: segfault\n",
            "    mech.paths.x_a__mutmut_1: survived\n",
        ]
    )
    states, error = mutation.survey(["mech.paths"], "/repo", run=fake_run)
    assert error is None
    assert states == {"survived": ["mech.paths.x_a__mutmut_1"]}
    assert fake_run.state["runs"] == 2  # pyright: ignore[reportFunctionMemberAccess]


def test_survey_with_include_killed_uses_the_all_true_argv():
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return (
            _completed(argv, 0) if argv[3] == "run" else _completed(argv, 0, stdout="")
        )

    mutation.survey(["mech.paths"], "/repo", run=fake_run, include_killed=True)
    assert seen[1] == list(tools.MUTMUT_RESULTS_ALL)


def test_survey_threads_root_and_environment_to_every_call():
    recorded_calls = []

    def fake_run(argv, **kwargs):
        recorded_calls.append(dict(kwargs))
        return (
            _completed(argv, 0) if argv[3] == "run" else _completed(argv, 0, stdout="")
        )

    sentinel_env = {"MUTMUT_TEST_TIME_MULTIPLIER": "2"}
    states, error = mutation.survey(
        ["mech.paths"], "/repo", run=fake_run, timeout=42, env=sentinel_env
    )

    assert states == {}
    assert error is None
    assert len(recorded_calls) == 2
    for kwargs in recorded_calls:
        assert kwargs["cwd"] == "/repo"
        assert kwargs["timeout"] == 42
        assert kwargs["env"] is sentinel_env


def test_survey_without_include_killed_uses_the_plain_argv():
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return (
            _completed(argv, 0) if argv[3] == "run" else _completed(argv, 0, stdout="")
        )

    mutation.survey(["mech.paths"], "/repo", run=fake_run)
    assert seen[1] == list(tools.MUTMUT_RESULTS)


def test_survey_rejects_explicit_xdist_config_before_launch(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["mech"]\npytest_add_cli_args = ["-n4"]\n'
    )

    def should_not_run(argv, **kwargs):
        raise AssertionError("conflicting configuration must block before mutmut")

    states, error = mutation.survey(["mech.paths"], str(tmp_path), run=should_not_run)

    assert states == {}
    assert error is not None and "Pawl owns serial execution" in error


def test_run_scoped_still_uses_the_plain_results_argv():
    # B14: survey's option must not leak into the gate's entry point.
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return (
            _completed(argv, 0) if argv[3] == "run" else _completed(argv, 0, stdout="")
        )

    mutation.run_scoped(["mech.paths"], "/repo", run=fake_run)
    assert seen[1] == list(tools.MUTMUT_RESULTS)


# --- REQ-B-4 row->mutation fingerprint ---------------------------------------

GENERATED_FIXTURE = (
    Path(__file__).parent / "fixtures" / "mutants_generated_paths.py.txt"
).read_text()

# Two byte-identical mutation sites in one function: the shape that makes a
# context-free delta hash ambiguous (196 of 1834 real mutants collide that way).
TWIN = """
def x_twin__mutmut_{tag}(run):
    first = run(
        ["gh", "issue", "list"],
        text={first},
        timeout=30,
    )
    second = run(
        ["gh", "issue", "edit"],
        text={second},
        timeout=60,
    )
    return first, second
"""

TWIN_SOURCE = "\n".join(
    [
        TWIN.format(tag="orig", first="True", second="True"),
        TWIN.format(tag="1", first="False", second="True"),
        TWIN.format(tag="2", first="True", second="False"),
    ]
)


def _generated(tmp_path, module, source):
    """Write `source` as `module`'s generated file; return the repo root."""
    parts = module.split(".")
    directory = tmp_path / "mutants"
    for part in parts[:-1]:
        directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{parts[-1]}.py").write_text(source)
    return str(tmp_path)


def test_fingerprint_of_real_generated_mutant_is_pinned(tmp_path):
    """Golden: the value IS the wire contract — 112 committed rows depend on it
    reproducing byte-for-byte across hosts and runs."""
    root = _generated(tmp_path, "mech.paths", GENERATED_FIXTURE)
    value, error = mutation.fingerprint("mech.paths.x__segments_match__mutmut_15", root)
    assert error is None
    assert value == "406497353fdc"
    assert mutation.FINGERPRINT_RE.match(value)


def test_fingerprint_separates_identical_deltas_at_different_sites(tmp_path):
    """The defect this exists to catch: an ordinal re-pointed to a sibling site."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    first, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    second, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_2", root)
    assert first != second

    def changed(mutant_id):
        emission, _ = mutation.mutant_delta(mutant_id, root)
        assert emission is not None
        return [line for line in emission if line[0] in "<>"]

    # ...and they differ *only* because of context: the changed lines themselves
    # are identical, which is exactly why a context-free hash cannot separate them.
    assert changed("mech.twin.x_twin__mutmut_1") == changed(
        "mech.twin.x_twin__mutmut_2"
    )


def test_fingerprint_ignores_comment_added_inside_the_context_window(tmp_path):
    """Commit 6f7d577 was comment-only; comment churn must not re-open rows."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    before, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    commented = _generated(
        tmp_path / "commented",
        "mech.twin",
        TWIN_SOURCE.replace("timeout=30,", "timeout=30,  # gh list is slow"),
    )
    after, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", commented)
    assert after == before


def test_fingerprint_ignores_reindentation_and_trailing_whitespace(tmp_path):
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    before, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    reflowed = _generated(
        tmp_path / "reflowed",
        "mech.twin",
        TWIN_SOURCE.replace("\n    ", "\n        ").replace(
            "timeout=30,", "timeout=30,   "
        ),
    )
    after, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", reflowed)
    assert after == before


def test_fingerprint_survives_an_edit_outside_the_context_window(tmp_path):
    """Rows must not all re-open every time their function is touched anywhere —
    13 ci_weekly.main rows stayed valid across three edits to that function."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    before, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    edited = _generated(
        tmp_path / "edited",
        "mech.twin",
        TWIN_SOURCE.replace(
            "    return first, second", "    log(first)\n    return first, second"
        ),
    )
    after, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", edited)
    assert after == before


def test_fingerprint_changes_when_the_context_window_changes(tmp_path):
    """The re-open trigger: the claim's subject moved, so the row must re-litigate."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    before, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    retimed = _generated(
        tmp_path / "retimed",
        "mech.twin",
        TWIN_SOURCE.replace("timeout=30,", "timeout=45,"),
    )
    after, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", retimed)
    assert after != before


def test_fingerprint_reports_an_ordinal_that_is_no_longer_generated(tmp_path):
    """REQ-B-3 case (b) territory: the ID must not silently yield a fingerprint."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    value, error = mutation.fingerprint("mech.twin.x_twin__mutmut_99", root)
    assert value is None
    assert error is not None
    assert "mech.twin.x_twin__mutmut_99" in error
    assert "99" in error


def test_fingerprint_reports_a_module_absent_from_the_generated_tree(tmp_path):
    value, error = mutation.fingerprint("mech.gone.x_f__mutmut_1", str(tmp_path))
    assert value is None
    assert error is not None
    assert "mech.gone" in error


def test_fingerprint_reports_an_unparseable_mutant_id(tmp_path):
    value, error = mutation.fingerprint("banana", str(tmp_path))
    assert value is None
    assert error is not None
    assert "banana" in error


def test_fingerprints_batch_keeps_good_rows_when_one_id_fails(tmp_path):
    """A single bad row must not cost the whole baseline its verdict."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    found, errors = mutation.fingerprints(
        [
            "mech.twin.x_twin__mutmut_1",
            "mech.twin.x_twin__mutmut_99",
            "mech.twin.x_twin__mutmut_2",
        ],
        root,
    )
    assert set(found) == {"mech.twin.x_twin__mutmut_1", "mech.twin.x_twin__mutmut_2"}
    assert set(errors) == {"mech.twin.x_twin__mutmut_99"}


def test_mutant_delta_shows_the_replaced_and_replacing_lines(tmp_path):
    """The mismatch reporter prints this next to the row's reason, so a human
    re-stamping a fingerprint is deciding, not just pasting a hash."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    emission, error = mutation.mutant_delta("mech.twin.x_twin__mutmut_1", root)
    assert error is None
    assert emission is not None
    assert "<text=True," in emission
    assert ">text=False," in emission


# mutmut mutates default arguments, so a row can name a mutant whose only
# changed line IS the signature — the extraction must start at the `def`.
SIGNATURE_SOURCE = """
def x_slot__mutmut_orig(root, timeout=600):
    proc = run(root)
    return proc, timeout


def x_slot__mutmut_1(root, timeout=601):
    proc = run(root)
    return proc, timeout


def x_slot__mutmut_2(root, timeout=600):
    proc = run(None)
    return proc, timeout
"""

# A "mutant" differing from the original only in a comment: normalization makes
# the two bodies identical, which must fail closed rather than fingerprint "".
COMMENT_ONLY_SOURCE = """
def x_ghost__mutmut_orig(a):
    return a  # original note


def x_ghost__mutmut_1(a):
    return a  # rewritten note
"""


def test_fingerprint_reports_a_missing_original_body(tmp_path):
    """A generated tree carrying the mutant but not `__mutmut_orig` must name
    that, not fingerprint the mutant against nothing. There is no baseline to
    diff against, so a silent success here would mint a fingerprint that no
    later run could reproduce."""
    root = _generated(
        tmp_path,
        "mech.orphan",
        "def x_gone__mutmut_1(a):\n    return a + 1\n",
    )
    value, error = mutation.fingerprint("mech.orphan.x_gone__mutmut_1", root)
    assert value is None
    assert error == (
        "mech.orphan.x_gone__mutmut_1: x_gone__mutmut_orig absent from the "
        "generated tree"
    )


def test_normalized_lines_degrades_when_the_body_will_not_tokenize():
    """Documented fallback: an untokenizable body keeps its comments rather
    than raising. Driven directly because it cannot be reached end-to-end —
    `_function_bodies` only yields slices of a file that already passed
    `ast.parse`, and every such slice tokenizes on its own (probed across
    nested defs, bracket and backslash continuations, and methods)."""
    out = mutation._normalized_lines(['x = "unterminated', "    return 1  # kept"])
    # Comment retained: proof the tokenizer bailed. On a tokenizable body the
    # `# kept` would have been stripped.
    assert out == ['x = "unterminated', "return 1 # kept"]


@given(
    st.lists(
        st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)),
            max_size=40,
        ),
        max_size=6,
    )
)
def test_normalized_lines_is_total(lines):
    """The real contract behind the tokenize fallback: normalization is total.

    It runs over whatever mutmut generated, so "no input raises" is the
    property that matters — an example-based test can only ever pin the one
    malformed shape its author thought of. The example above proves the
    fallback branch exists; this proves nothing escapes it.

    Every returned line is non-empty and fully stripped, which is what the
    fingerprint hash is computed over.
    """
    out = mutation._normalized_lines(lines)
    assert all(c and c == c.strip() for c in out)


def test_fingerprint_of_a_signature_line_mutation(tmp_path):
    """Golden. Drop the `def` line from extraction and this delta is empty —
    a default-argument mutant would become unfingerprintable."""
    root = _generated(tmp_path, "mech.slot", SIGNATURE_SOURCE)
    value, error = mutation.fingerprint("mech.slot.x_slot__mutmut_1", root)
    assert error is None
    assert value == "3e31c3f2236c"


def test_fingerprint_of_a_second_line_mutation_keeps_the_signature_as_context(
    tmp_path,
):
    """Golden. The leading-context window must reach index 0, or the `def` line
    is silently dropped from the context of every early mutation."""
    root = _generated(tmp_path, "mech.slot", SIGNATURE_SOURCE)
    value, error = mutation.fingerprint("mech.slot.x_slot__mutmut_2", root)
    assert error is None
    assert value == "a07206c573ca"
    emission, _ = mutation.mutant_delta("mech.slot.x_slot__mutmut_2", root)
    assert emission is not None
    assert "-def FN(root, timeout=600):" in emission


def test_fingerprint_ignores_internal_whitespace_runs(tmp_path):
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    before, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", root)
    respaced = _generated(
        tmp_path / "respaced",
        "mech.twin",
        TWIN_SOURCE.replace('["gh", "issue", "list"],', '["gh",  "issue",  "list"],'),
    )
    after, _ = mutation.fingerprint("mech.twin.x_twin__mutmut_1", respaced)
    assert after == before


def test_fingerprint_fails_closed_when_the_normalized_delta_is_empty(tmp_path):
    """A mutant indistinguishable after normalization must not silently share a
    fingerprint with every other such mutant."""
    root = _generated(tmp_path, "mech.ghost", COMMENT_ONLY_SOURCE)
    value, error = mutation.fingerprint("mech.ghost.x_ghost__mutmut_1", root)
    assert value is None
    assert error is not None
    assert error == "mech.ghost.x_ghost__mutmut_1: normalized delta is empty"


def test_fingerprint_resolves_a_package_module_to_its_init_file(tmp_path):
    """`mech/__init__.py` is a source module, and mutmut names its mutants
    `mech.x_f__mutmut_N` — the dotted path resolves to no `mech.py`."""
    directory = tmp_path / "mutants" / "mech" / "pkg"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(TWIN_SOURCE)
    value, error = mutation.fingerprint("mech.pkg.x_twin__mutmut_1", str(tmp_path))
    assert error is None
    assert value is not None
    assert mutation.FINGERPRINT_RE.match(value)


def test_fingerprint_error_names_the_exception_type(tmp_path):
    """Fail-closed messages must say what broke, or a gate failure is unactionable."""
    root = _generated(tmp_path, "mech.broken", "def x_f__mutmut_1(:\n")
    value, error = mutation.fingerprint("mech.broken.x_f__mutmut_1", root)
    assert value is None
    assert error is not None
    assert "SyntaxError" in error


def test_fingerprints_batch_continues_past_an_unparseable_id(tmp_path):
    """One malformed row must not cost every row after it its verdict."""
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    found, errors = mutation.fingerprints(
        ["banana", "mech.twin.x_twin__mutmut_1"], root
    )
    assert set(found) == {"mech.twin.x_twin__mutmut_1"}
    assert set(errors) == {"banana"}


def test_fingerprints_batch_continues_past_a_missing_module(tmp_path):
    root = _generated(tmp_path, "mech.twin", TWIN_SOURCE)
    found, errors = mutation.fingerprints(
        ["mech.gone.x_f__mutmut_1", "mech.twin.x_twin__mutmut_1"], root
    )
    assert set(found) == {"mech.twin.x_twin__mutmut_1"}
    assert set(errors) == {"mech.gone.x_f__mutmut_1"}


def test_regenerate_tree_argv_comes_from_the_tools_table():
    """REQ-ARCH-9: the sentinel filter is substituted into tools.MUTMUT_RUN,
    never assembled inline."""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = list(argv)
        return _completed(argv, 0)

    mutation.regenerate_tree("/repo", run=fake_run)
    assert recorded["argv"] == [
        a.replace("{module}", mutation.COLLECT_ONLY_FILTER)
        for a in mutation.tools.MUTMUT_RUN
    ]


def test_regenerate_tree_rejects_explicit_xdist_config_before_launch(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["mech"]\npytest_add_cli_args = ["-n4"]\n'
    )

    def should_not_run(argv, **kwargs):
        raise AssertionError("conflicting configuration must block before mutmut")

    error = mutation.regenerate_tree(str(tmp_path), run=should_not_run)

    assert error is not None and "Pawl owns serial execution" in error


def test_regenerate_tree_run_call_full_kwargs_exact():
    """Pins every subprocess kwarg and the default timeout, so a mutant that
    drops or flips any single one is caught."""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs)
        return _completed(argv, 0)

    sentinel_env = {"X": "1"}
    assert mutation.regenerate_tree("/repo", run=fake_run, env=sentinel_env) is None
    assert recorded == {
        "cwd": "/repo",
        "capture_output": True,
        "text": True,
        "timeout": 600,  # REQ-ARCH-9 default, not merely "some number"
        "check": False,
        "env": sentinel_env,
    }


def test_regenerate_tree_treats_the_zero_mutants_signature_as_success():
    """The mechanism: there is no collect-only command, so the no-match abort is
    how the tree gets rebuilt. Matched on text, never on exit code."""

    def fake_run(argv, **kwargs):
        return _completed(
            argv, 1, stderr=f"AssertionError: {mutation.ZERO_MUTANTS_SIGNATURE}\n"
        )

    assert mutation.regenerate_tree("/repo", run=fake_run) is None


def test_regenerate_tree_reports_a_nonzero_exit_without_the_signature():
    def fake_run(argv, **kwargs):
        return _completed(argv, 1, stderr="Traceback: something else broke\n")

    error = mutation.regenerate_tree("/repo", run=fake_run)
    assert error is not None
    assert error.startswith("mutants tree regeneration failed: exit 1")


def test_regenerate_tree_error_carries_the_captured_diagnostic():
    """A bare exit code sent one investigation to the wrong place.

    Reproduces the real failure: four test files deleted from `tests/` on
    2026-07-25 were still in `mutants/tests/` (mutmut copies with
    `dirs_exist_ok=True`, which never deletes), so mutmut's collection died on
    a stale import. The adapter captured the cause and discarded it, reporting
    only `exit 1` on a repo whose own suite was green.
    """
    captured = (
        "tests/test_edit_guard.py:472\n"
        "AttributeError: module 'mech.paths' has no attribute 'DENY_SURFACE'\n"
        "Failed to collect list of tests\n"
    )

    def fake_run(argv, **kwargs):
        return _completed(argv, 1, stdout=captured)

    error = mutation.regenerate_tree("/repo", run=fake_run)
    assert error is not None
    assert "Failed to collect list of tests" in error
    assert "DENY_SURFACE" in error


def test_regenerate_tree_error_is_bounded_and_strips_ansi():
    """mutmut prints one line per mutated file, so the tail has to be bounded —
    an unbounded splice of captured output into a gate error is how 783 lines of
    tool output once reached a commit message. pytest colours its summary, and
    those escapes would reach whatever renders the error."""

    def fake_run(argv, **kwargs):
        noise = "".join(f"mech/module_{i}.py\n" for i in range(500))
        return _completed(
            argv, 1, stdout=noise + "\x1b[31mERROR\x1b[0m collecting tests\n"
        )

    error = mutation.regenerate_tree("/repo", run=fake_run)
    assert error is not None
    assert error.count("\n") <= mutation.DIAGNOSTIC_TAIL_LINES
    assert "ERROR collecting tests" in error
    assert "\x1b" not in error


def test_regenerate_tree_error_names_the_exception_type():
    def fake_run(argv, **kwargs):
        raise OSError("mutmut binary not found")

    error = mutation.regenerate_tree("/repo", run=fake_run)
    assert error is not None
    assert "OSError" in error


def _repetitive_source():
    """A function long enough (>=200 normalized lines) for difflib's autojunk
    heuristic to engage, with the mutated line buried inside the repeated run."""

    def body(marker):
        return "\n".join(
            ["    x = 1"] * 120 + [f"    z = {marker}"] + ["    x = 1"] * 120
        )

    return (
        f"def x_long__mutmut_orig(a):\n{body(9)}\n\n\n"
        f"def x_long__mutmut_1(a):\n{body(8)}\n"
    )


def test_fingerprint_isolates_a_mutation_buried_in_a_run_of_repeated_lines(tmp_path):
    """difflib treats a line filling >1% of a 200+ line function as junk unless
    autojunk is off; with it on, this one-line change reads as a 121-line
    replacement and the fingerprint stops describing the mutation."""
    root = _generated(tmp_path, "mech.long", _repetitive_source())
    emission, error = mutation.mutant_delta("mech.long.x_long__mutmut_1", root)
    assert error is None
    assert emission is not None
    assert [line for line in emission if line[0] == "<"] == ["<z = 9"]
    assert [line for line in emission if line[0] == ">"] == [">z = 8"]


def test_mutant_delta_reports_the_error_for_its_own_id(tmp_path):
    emission, error = mutation.mutant_delta("banana", str(tmp_path))
    assert emission is None
    assert error is not None
    assert "banana" in error


# --- REQ-B-4 accounting property ---------------------------------------------
#
# IDs reach `fingerprints` from two places that can disagree with the generated
# tree: mutmut's own result lines, and hand-written MUTANTS.md rows. So the
# input is "any string", and the contract that keeps a stale row from being
# skipped is total accounting — docstring: "an unresolvable ID never silently
# yields an empty delta".
_MUTANT_IDS = st.one_of(
    st.sampled_from(
        (
            "mech.twin.x_twin__mutmut_1",  # resolvable
            "mech.twin.x_twin__mutmut_2",  # resolvable, sibling site
            "  mech.twin.x_twin__mutmut_1  ",  # padded: keyed by the raw string
            "mech.twin.x_twin__mutmut_99",  # ordinal not generated
            "mech.twin.absent__mutmut_1",  # function absent
            "mech.gone.f__mutmut_1",  # module absent from the tree
            "mech.twin.x_twin",  # no ordinal suffix
            "",
        )
    ),
    st.text(max_size=20),
)


@given(st.lists(_MUTANT_IDS, max_size=6))
def test_fingerprints_accounts_for_every_requested_id(mutant_ids):
    with tempfile.TemporaryDirectory() as tmp:
        root = _generated(Path(tmp), "mech.twin", TWIN_SOURCE)
        found, errors = mutation.fingerprints(mutant_ids, root)

    assert set(found) | set(errors) == set(mutant_ids)
    assert set(found).isdisjoint(errors)
    # Every value the generator emits must satisfy the grammar `baseline`
    # enforces on the fifth cell — the two are the same wire format.
    assert all(mutation.FINGERPRINT_RE.match(value) for value in found.values())
