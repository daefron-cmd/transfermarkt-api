import json
import subprocess
from pathlib import Path

import pytest

from mech import selftest, tools


def _write_compatibility_inputs(root: Path) -> None:
    (root / "mech" / "tests").mkdir(parents=True)
    (root / "mech" / "gate.py").write_text("x = 1\n", encoding="utf-8")
    (root / "mech" / "tests" / "test_gate.py").write_text(
        "def test_x(): assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def test_compatibility_digest_changes_with_mech_and_shared_environment(tmp_path: Path):
    _write_compatibility_inputs(tmp_path)
    original = selftest.compatibility_digest(str(tmp_path))
    assert (
        original == "1a29c40479813c465753bc4131c01eb907f3d9fb106684206fe827fb3ea4506d"
    )

    (tmp_path / "mech" / "gate.py").write_text("x = 2\n", encoding="utf-8")
    assert selftest.compatibility_digest(str(tmp_path)) != original

    (tmp_path / "mech" / "gate.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    assert selftest.compatibility_digest(str(tmp_path)) != original


def test_compatibility_digest_ignores_runtime_caches_and_the_record(tmp_path: Path):
    _write_compatibility_inputs(tmp_path)
    original = selftest.compatibility_digest(str(tmp_path))

    cache = tmp_path / "mech" / "__pycache__"
    cache.mkdir()
    (cache / "gate.pyc").write_bytes(b"cache")
    (tmp_path / "mech" / ".DS_Store").write_bytes(b"finder")
    (tmp_path / "mech" / "stale.pyc").write_bytes(b"bytecode")
    (tmp_path / "mech" / "stale.pyo").write_bytes(b"optimized")
    (tmp_path / ".pawl-selftest.json").write_text("changed", encoding="utf-8")

    assert selftest.compatibility_digest(str(tmp_path)) == original


def test_compatibility_digest_tracks_repo_surface_above_nested_project(tmp_path: Path):
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved git; fixture only
        cwd=tmp_path,
        check=True,
    )
    project = tmp_path / "service"
    project.mkdir()
    _write_compatibility_inputs(project)
    hooks = tmp_path / ".codex"
    hooks.mkdir()
    hook_file = hooks / "hooks.json"
    hook_file.write_text('{"hooks": {}}\n', encoding="utf-8")
    original = selftest.compatibility_digest(str(project))

    hook_file.write_text('{"hooks": {"Stop": []}}\n', encoding="utf-8")

    assert selftest.compatibility_digest(str(project)) != original


def test_record_round_trip_and_staleness(tmp_path: Path):
    _write_compatibility_inputs(tmp_path)
    record = ".pawl-selftest.json"

    assert selftest.record_error(str(tmp_path), record) == (
        "Pawl self-test record is missing: .pawl-selftest.json"
    )
    selftest.write_record(str(tmp_path), record)
    assert selftest.record_error(str(tmp_path), record) is None

    data = json.loads((tmp_path / record).read_text(encoding="utf-8"))
    assert data == {
        "schema": 1,
        "digest": selftest.compatibility_digest(str(tmp_path)),
    }

    (tmp_path / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n# changed\n',
        encoding="utf-8",
    )
    assert selftest.record_error(str(tmp_path), record) == (
        "Pawl self-test record is stale: .pawl-selftest.json — run "
        "`uv run python -m mech.selftest --record`"
    )


def test_record_path_is_opt_in_and_rejects_escape():
    assert selftest.configured_record('[tool.mech]\npyright_scope = "whole"\n') is None
    assert (
        selftest.configured_record(
            '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n'
        )
        == ".pawl-selftest.json"
    )
    with pytest.raises(selftest.RecordConfigError, match="relative path"):
        selftest.configured_record(
            '[tool.mech]\npawl_selftest_record = "../outside.json"\n'
        )


def test_record_config_names_parse_errors_and_every_unsafe_value():
    with pytest.raises(selftest.RecordConfigError) as error:
        selftest.configured_record("[")
    assert str(error.value).startswith("pyproject.toml is unparseable:")

    for value in ('""', "1", '"/outside.json"'):
        with pytest.raises(selftest.RecordConfigError) as error:
            selftest.configured_record(f"[tool.mech]\npawl_selftest_record = {value}\n")
        assert str(error.value) == selftest.RECORD_PATH_ERROR


def test_record_config_normalises_windows_separators():
    separator = chr(92)
    text = f"[tool.mech]\npawl_selftest_record = 'records{separator}selftest.json'\n"
    assert selftest.configured_record(text) == "records/selftest.json"


def test_nested_record_is_compact_deterministic_and_creates_parent(tmp_path: Path):
    _write_compatibility_inputs(tmp_path)
    digest = selftest.compatibility_digest(str(tmp_path))

    selftest.write_record(str(tmp_path), "records/selftest.json")

    assert (tmp_path / "records" / "selftest.json").read_text() == (
        f'{{"digest":"{digest}","schema":1}}\n'
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 2, "digest": "a" * 64},
        {"schema": 1, "digest": 42},
    ],
)
def test_record_error_rejects_each_unsupported_schema_shape(tmp_path: Path, payload):
    record = ".pawl-selftest.json"
    (tmp_path / record).write_text(json.dumps(payload))

    assert selftest.record_error(str(tmp_path), record) == (
        "Pawl self-test record has unsupported schema: .pawl-selftest.json"
    )


def test_record_error_names_malformed_json_exception(tmp_path: Path):
    record = ".pawl-selftest.json"
    (tmp_path / record).write_text("{")

    error = selftest.record_error(str(tmp_path), record)

    assert error is not None
    assert error.startswith("Pawl self-test record is unreadable: JSONDecodeError:")


def test_run_selftest_uses_explicit_path_and_scrubbed_environment(tmp_path: Path):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    assert selftest.run_selftest(str(tmp_path), run=fake_run) == 0
    assert calls == [
        (
            [
                "uv",
                "run",
                "pytest",
                "mech/tests",
                "-q",
                tools.REQUIRED_PYTEST_SEED_ARG,
            ],
            {
                "cwd": str(tmp_path),
                "check": False,
                "env": tools.scrubbed_env(),
            },
        )
    ]


def test_run_selftest_names_missing_path_and_empty_collection(tmp_path: Path, capsys):
    def missing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 4)

    assert selftest.run_selftest(str(tmp_path), run=missing) == 1
    assert capsys.readouterr().err == (
        "Pawl self-test could not collect mech/tests (pytest exit 4)\n"
    )

    def empty(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5)

    assert selftest.run_selftest(str(tmp_path), run=empty) == 1
    assert capsys.readouterr().err == (
        "Pawl self-test collected no tests from mech/tests (pytest exit 5)\n"
    )


def test_run_selftest_names_exceptions_and_ordinary_red_exit(tmp_path: Path, capsys):
    def exploding(argv, **kwargs):
        raise RuntimeError("boom")

    assert selftest.run_selftest(str(tmp_path), run=exploding) == 1
    assert capsys.readouterr().err == (
        "Pawl self-test could not run: RuntimeError: boom\n"
    )

    def red(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 7)

    assert selftest.run_selftest(str(tmp_path), run=red) == 1
    assert capsys.readouterr().err == "Pawl self-test failed with exit 7\n"


def test_read_pyproject_returns_content_or_empty(tmp_path: Path):
    assert selftest._read_pyproject(str(tmp_path)) == ""
    (tmp_path / "pyproject.toml").write_text("[tool.mech]\n", encoding="utf-8")
    assert selftest._read_pyproject(str(tmp_path)) == "[tool.mech]\n"


def test_check_record_covers_opt_out_invalid_config_and_missing_record(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mech]\npyright_scope = "whole"\n', encoding="utf-8"
    )
    assert selftest.check_record(str(tmp_path)) is None

    (tmp_path / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = "../outside.json"\n',
        encoding="utf-8",
    )
    assert selftest.check_record(str(tmp_path)) == (
        "Pawl self-test configuration invalid: " + selftest.RECORD_PATH_ERROR
    )

    (tmp_path / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = ".pawl-selftest.json"\n',
        encoding="utf-8",
    )
    assert selftest.check_record(str(tmp_path)) == (
        "Pawl self-test record is missing: .pawl-selftest.json"
    )


def test_run_and_record_writes_only_after_a_green_selftest(tmp_path: Path, capsys):
    _write_compatibility_inputs(tmp_path)

    def red(argv, **kwargs):
        assert kwargs["cwd"] == str(tmp_path)
        return subprocess.CompletedProcess(argv, 1)

    assert selftest.run_and_record(str(tmp_path), run=red) == 1
    assert not (tmp_path / ".pawl-selftest.json").exists()
    assert capsys.readouterr().err == "Pawl self-test failed with exit 1\n"

    def green(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0)

    assert selftest.run_and_record(str(tmp_path), run=green) == 0
    assert selftest.record_error(str(tmp_path), ".pawl-selftest.json") is None


def test_run_and_record_names_invalid_and_missing_record_config(tmp_path: Path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mech]\npawl_selftest_record = "../outside.json"\n'
    )
    assert selftest.run_and_record(str(tmp_path)) == 1
    assert capsys.readouterr().err == (
        f"Pawl self-test configuration invalid: {selftest.RECORD_PATH_ERROR}\n"
    )

    (tmp_path / "pyproject.toml").write_text('[tool.mech]\npyright_scope = "whole"\n')
    assert selftest.run_and_record(str(tmp_path)) == 1
    assert capsys.readouterr().err == (
        "Pawl self-test recording is not configured at "
        "[tool.mech].pawl_selftest_record\n"
    )


def test_main_routes_each_mode_through_the_resolved_root(monkeypatch, capsys):
    calls = []

    def repo_root(cwd):
        assert cwd == "/cwd"
        return "/repo"

    monkeypatch.setattr(selftest.os, "getcwd", lambda: "/cwd")
    monkeypatch.setattr(selftest.paths, "repo_root", repo_root)
    monkeypatch.setattr(
        selftest, "run_selftest", lambda root: calls.append(("run", root)) or 17
    )
    monkeypatch.setattr(
        selftest,
        "run_and_record",
        lambda root: calls.append(("record", root)) or 18,
    )
    monkeypatch.setattr(selftest, "check_record", lambda root: None)

    assert selftest.main([]) == 17
    assert selftest.main(["--record"]) == 18
    assert selftest.main(["--check"]) == 0
    assert calls == [("run", "/repo"), ("record", "/repo")]

    monkeypatch.setattr(selftest, "check_record", lambda root: f"bad record: {root}")
    assert selftest.main(["--check"]) == 1
    assert capsys.readouterr().err == "bad record: /repo\n"

    with pytest.raises(SystemExit) as error:
        selftest.main(["--record", "--check"])
    assert error.value.code == 2


def test_main_routes_nested_repo_modes_through_the_project_cwd(monkeypatch, capsys):
    calls = []

    def project_root(cwd, repository_root):
        calls.append(("resolve", cwd, repository_root))
        return "/repo/service"

    monkeypatch.setattr(selftest.os, "getcwd", lambda: "/repo/service")
    monkeypatch.setattr(selftest.paths, "repo_root", lambda _cwd: "/repo")
    monkeypatch.setattr(selftest.paths, "project_root", project_root)
    monkeypatch.setattr(
        selftest, "run_selftest", lambda root: calls.append(("run", root)) or 0
    )
    monkeypatch.setattr(
        selftest,
        "run_and_record",
        lambda root: calls.append(("record", root)) or 0,
    )
    monkeypatch.setattr(
        selftest,
        "check_record",
        lambda root: calls.append(("check", root)) or "stale",
    )

    assert selftest.main([]) == 0
    assert selftest.main(["--record"]) == 0
    assert selftest.main(["--check"]) == 1
    assert calls == [
        ("resolve", "/repo/service", "/repo"),
        ("run", "/repo/service"),
        ("resolve", "/repo/service", "/repo"),
        ("record", "/repo/service"),
        ("resolve", "/repo/service", "/repo"),
        ("check", "/repo/service"),
    ]
    assert capsys.readouterr().err == "stale\n"
