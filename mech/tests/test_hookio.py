import io
import json
from pathlib import Path

from mech import hookio

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"
RESPONSE_FIXTURES = FIXTURES / "responses"


def test_read_event_parses_every_fixture():  # B-29 (shape layer)
    for fx in sorted(FIXTURES.glob("*.json")):
        event = hookio.read_event(io.StringIO(fx.read_text()))
        assert "hook_event_name" in event


def test_system_message_shape(capsys):
    hookio.system_message("3 failed")
    assert json.loads(capsys.readouterr().out) == {"systemMessage": "3 failed"}


def test_stop_block_shape(capsys):
    hookio.stop_block("collection error")
    assert json.loads(capsys.readouterr().out) == {
        "decision": "block",
        "reason": "collection error",
    }


def test_emitters_match_response_fixtures_exactly(capsys):
    """Emitters produce JSON matching response fixtures exactly."""
    # Test stop_block emitter
    code = hookio.stop_block("collection error")
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    stop_out = json.loads(captured.out)
    stop_fixture = json.loads((RESPONSE_FIXTURES / "stop_block.json").read_text())
    assert stop_out == stop_fixture

    # Test system_message emitter
    code = hookio.system_message("3 failed")
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    msg_out = json.loads(captured.out)
    msg_fixture = json.loads(
        (RESPONSE_FIXTURES / "root_systemmessage.json").read_text()
    )
    assert msg_out == msg_fixture


def test_apply_patch_fixture_in_corpus_glob():
    """The Codex apply_patch fixture exists and can be parsed."""
    fixture_names = {p.name for p in FIXTURES.glob("*.json")}
    assert "pretooluse_apply_patch.json" in fixture_names
    patch_fixture = FIXTURES / "pretooluse_apply_patch.json"
    event = hookio.read_event(io.StringIO(patch_fixture.read_text()))
    assert event["hook_event_name"] == "PreToolUse"
    assert event["tool_name"] == "apply_patch"
    assert "*** Update File: x.py" in event["tool_input"]["command"]
