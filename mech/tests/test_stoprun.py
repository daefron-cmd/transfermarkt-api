import io
import json
import subprocess
from pathlib import Path

from mech import stoprun, tools

RESPONSE_FIXTURES = Path(__file__).parent / "fixtures" / "hooks" / "responses"

OUT_FAIL = """Using --randomly-seed=373657856
FAILED tests/test_a.py::test_one - AssertionError
FAILED tests/test_a.py::test_two - AssertionError
2 failed, 3 passed in 1.2s
"""


# Real pytest output when colour is forced (FORCE_COLOR=1 / PY_COLORS=1, both
# routine in dotfiles for node tooling and inherited by Codex hook commands).
# The SGR codes are what pytest actually emits; the seed line is uncoloured,
# which is why the broken summary still looked complete.
OUT_FAIL_COLOURED = (
    "Using --randomly-seed=373657856\n"
    "\x1b[31mFAILED\x1b[0m tests/test_a.py::\x1b[1mtest_one\x1b[0m - AssertionError\n"
    "\x1b[31mFAILED\x1b[0m tests/test_a.py::\x1b[1mtest_two\x1b[0m - AssertionError\n"
    "\x1b[31m2 failed, 3 passed in 1.2s\x1b[0m\n"
)


def test_green_is_silent():  # B-14 half
    assert stoprun.summarize(0, "all good", False) == ("silent", None)


def test_coloured_failures_are_counted_and_named():
    """A red suite must not be reported as green. Colour prefixes each FAILED
    line with an SGR escape, so a `^FAILED`-anchored match saw zero failures and
    said so out loud — an affirmatively false report, not silence."""
    action, message = stoprun.summarize(1, OUT_FAIL_COLOURED, False)
    assert action == "message"
    assert message is not None
    assert "2 failed/errored" in message
    assert "tests/test_a.py::test_one" in message
    assert "tests/test_a.py::test_two" in message


def test_coloured_and_plain_output_summarize_identically():
    """Colour is a rendering detail; it must not change the verdict at all."""
    assert stoprun.summarize(1, OUT_FAIL_COLOURED, False) == stoprun.summarize(
        1, OUT_FAIL, False
    )


def test_colon_separated_sgr_failures_are_counted():
    """ITU-T T.416 SGR parameters may be colon-separated (\\x1b[38:5:196m) and
    some tools emit that form; it must summarize identically to the
    semicolon form, not hide the FAILED line and report a red suite clean."""
    colon = "\x1b[38:5:196mFAILED\x1b[0m tests/t.py::t - AssertionError\n1 failed\n"
    semi = "\x1b[38;5;196mFAILED\x1b[0m tests/t.py::t - AssertionError\n1 failed\n"
    assert stoprun.summarize(1, colon, False) == stoprun.summarize(1, semi, False)
    _, message = stoprun.summarize(1, colon, False)
    assert message is not None
    assert "1 failed/errored" in message


def test_coloured_infrastructure_tail_carries_no_escapes():
    """The infra-red branch pastes a 600-char tail into a message a human
    reads; raw escapes would garble the terminal it lands in."""
    _, message = stoprun.summarize(3, OUT_FAIL_COLOURED, False)
    assert message is not None
    assert "\x1b[" not in message


def test_failures_report_never_block():  # B-12
    action, msg = stoprun.summarize(1, OUT_FAIL, False)
    assert action == "message"
    assert msg is not None
    assert "2 failed" in msg and "test_one" in msg
    assert "373657856" in msg  # B-33


def test_seed_unavailable_when_absent():  # B-33
    action, msg = stoprun.summarize(1, "FAILED tests/t.py::x\n1 failed", False)
    assert action == "message"
    assert msg is not None and "seed unavailable" in msg


def test_infra_red_blocks_once():  # B-13
    action, msg = stoprun.summarize(3, "INTERNALERROR traceback", False)
    assert action == "block"
    assert msg is not None and "INTERNALERROR" in msg


def test_infra_red_reports_when_already_continued():  # B-13
    action, msg = stoprun.summarize(3, "INTERNALERROR", True)
    assert action == "message"
    assert msg is not None and "seed unavailable" in msg


def test_no_tests_collected_warns():  # B-14
    action, msg = stoprun.summarize(5, "", False)
    assert action == "message"
    assert msg is not None and "no tests" in msg.lower()


def test_unknown_exit_code_is_infra_red():  # B-13 extension
    assert stoprun.summarize(127, "boom", False)[0] == "block"


def test_exit1_message_exact():  # REQ-T2-2 extraction contract, byte-exact
    assert stoprun.summarize(1, OUT_FAIL, False) == (
        "message",
        "pytest: 2 failed/errored | "
        "first failures: tests/test_a.py::test_one, tests/test_a.py::test_two | "
        "Using --randomly-seed=373657856",
    )


def test_exit1_without_summary_lines_message_exact():  # n/a + seed fallbacks
    assert stoprun.summarize(1, "", False) == (
        "message",
        "pytest: 0 failed/errored | first failures: n/a | seed unavailable",
    )


def test_exit1_lists_first_five_of_six():  # REQ-T2-2: first 5 in order
    out = "\n".join(f"FAILED tests/t.py::t{i} - boom" for i in range(1, 7))
    assert stoprun.summarize(1, out, False) == (
        "message",
        "pytest: 6 failed/errored | "
        "first failures: tests/t.py::t1, tests/t.py::t2, tests/t.py::t3, "
        "tests/t.py::t4, tests/t.py::t5 | seed unavailable",
    )


def test_exit5_message_exact():  # B-14
    assert stoprun.summarize(5, "", False) == ("message", "pytest: no tests collected")


def test_infra_red_tail_is_exactly_last_600():  # B-13 tail boundary
    assert stoprun.summarize(2, "a" * 601, False) == (
        "block",
        f"pytest infrastructure red (exit 2): {'a' * 600} | seed unavailable",
    )


# --- Cross-review additions (mandatory, test-first) -------------------------
# Wire-contract tests for hook_stop.main, driven via the stdin/run seams.
# Never monkeypatch module privates.


def _envelope(*, include_stop_hook_active: bool, stop_hook_active: bool = False) -> str:
    payload = {
        "session_id": "s1",
        "transcript_path": None,
        "cwd": ".",
        "hook_event_name": "Stop",
    }
    if include_stop_hook_active:
        payload["stop_hook_active"] = stop_hook_active
    return json.dumps(payload)


def _run(rc: int, out: str):
    def _fake(argv, **_kw):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    return _fake


def test_hook_stop_green_is_silent_and_invocation_is_pinned(
    monkeypatch, capsys
):  # REQ-T2-1 wire: green = exit 0, no output; the suite invocation is data
    from mech import hook_stop

    calls = []

    def _recorder(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="all passed", stderr="")

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_envelope(include_stop_hook_active=True))
    )

    code = hook_stop.main(run=_recorder)

    assert code == 0
    assert capsys.readouterr().out == ""
    # exact invocation contract: full suite via uv, output captured as text,
    # nonzero exit surfaced to summarize (never raised), ambient pytest config
    # scrubbed exactly as the commit gate scrubs it
    ((argv, kwargs),) = calls
    assert argv == ["uv", "run", "pytest"]
    assert tools.REQUIRED_PYTEST_SEED_ARG not in argv
    assert set(kwargs) == {"capture_output", "text", "check", "env"}
    assert (kwargs["capture_output"], kwargs["text"], kwargs["check"]) == (
        True,
        True,
        False,
    )


def test_hook_stop_scrubs_ambient_pytest_config(monkeypatch):
    """The Stop hook is the only always-on check in the stack. An ambient
    PYTEST_ADDOPTS in the launching shell — which a Codex hook command inherits —
    otherwise reaches it and can neuter the run outright."""
    from mech import hook_stop

    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    monkeypatch.setenv("KEEP_ME", "1")
    calls = []

    def _recorder(argv, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="all passed", stderr="")

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_envelope(include_stop_hook_active=True))
    )

    hook_stop.main(run=_recorder)

    assert "PYTEST_ADDOPTS" not in calls[0]["env"]
    assert calls[0]["env"].get("KEEP_ME") == "1"


def test_infra_red_reports_when_field_absent(
    monkeypatch, capsys
):  # B-13 absent-default, wire
    from mech import hook_stop

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_envelope(include_stop_hook_active=False))
    )

    code = hook_stop.main(run=_run(3, "INTERNALERROR traceback"))

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    # absent stop_hook_active defaults True: never block
    assert "decision" not in payload
    assert payload == {"systemMessage": payload["systemMessage"]}
    assert "seed unavailable" in payload["systemMessage"]


def test_hook_stop_exit1_matches_wire_fixture(monkeypatch, capsys):  # B-12 wire
    from mech import hook_stop

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_envelope(include_stop_hook_active=True, stop_hook_active=False)),
    )

    code = hook_stop.main(run=_run(1, OUT_FAIL))

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    fixture = json.loads((RESPONSE_FIXTURES / "root_systemmessage.json").read_text())
    assert set(payload.keys()) == set(fixture.keys())  # systemMessage shape
    assert "decision" not in payload  # no block document is emitted
    assert "2 failed" in payload["systemMessage"]
    assert "test_one" in payload["systemMessage"]
    assert "373657856" in payload["systemMessage"]


def test_hook_stop_block_document_shape(monkeypatch, capsys):  # B-13 wire
    from mech import hook_stop

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(_envelope(include_stop_hook_active=True, stop_hook_active=False)),
    )

    code = hook_stop.main(run=_run(3, "INTERNALERROR traceback"))

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    fixture = json.loads((RESPONSE_FIXTURES / "stop_block.json").read_text())
    assert set(payload.keys()) == set(fixture.keys())
    assert payload["decision"] == fixture["decision"] == "block"
    assert "INTERNALERROR" in payload["reason"]


def test_hook_stop_outer_crash_exits_0(monkeypatch, capsys):  # fail-open
    from mech import hook_stop

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))

    def _unexpected(argv, **_kw):
        raise AssertionError("run should never be reached on an outer crash")

    code = hook_stop.main(run=_unexpected)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "" and captured.err == ""
