import os
import subprocess

from mech import paths


def test_repo_root_none_outside_git(tmp_path):
    assert paths.repo_root(str(tmp_path)) is None


def test_repo_root_returns_toplevel_in_real_repo(tmp_path):
    subprocess.run(
        ["git", "init", "-q"],  # noqa: S607  # PATH-resolved `git`; test fixture only
        cwd=tmp_path,
        check=True,
    )
    assert paths.repo_root(str(tmp_path)) == os.path.realpath(str(tmp_path))


def test_project_root_keeps_a_nested_working_directory_inside_the_repo(
    tmp_path, monkeypatch
):
    project = tmp_path / "service"
    child = project / "pkg"
    child.mkdir(parents=True)
    marker = project / "pyproject.toml"
    marker.write_text("[tool.mech]\n", encoding="utf-8")
    expected_marker = os.path.realpath(marker)
    monkeypatch.setattr(
        paths.os.path,
        "isfile",
        lambda candidate: os.path.realpath(candidate) == expected_marker,
    )

    assert paths.project_root(str(child), str(tmp_path)) == os.path.realpath(project)


def test_project_root_uses_repo_pyproject_from_an_internal_directory(tmp_path):
    child = tmp_path / "mech" / "tests"
    child.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[tool.mech]\n", encoding="utf-8")

    assert paths.project_root(str(child), str(tmp_path)) == os.path.realpath(tmp_path)


def test_project_root_falls_back_when_cwd_is_not_inside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()

    assert paths.project_root(str(outside), str(repo)) == os.path.realpath(repo)


def test_repo_root_invocation_contract_exact(monkeypatch, tmp_path):
    calls = []

    def recorder(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="/repo\n", stderr="")

    monkeypatch.setattr(subprocess, "run", recorder)
    assert paths.repo_root(str(tmp_path)) == "/repo"
    assert calls == [
        (
            ["git", "rev-parse", "--show-toplevel"],
            {
                "cwd": str(tmp_path),
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            },
        )
    ]


def test_repo_root_none_when_git_is_not_installed(monkeypatch, tmp_path):
    """`git` absent from PATH must degrade to "no repo", not raise. Both callers
    treat None as fail-closed: commit_gate refuses to certify, and validation
    reports UNARMED — an escaping OSError would instead crash the pre-commit
    hook with a traceback."""

    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert paths.repo_root(str(tmp_path)) is None


def test_repo_root_none_when_git_exceeds_the_timeout(monkeypatch, tmp_path):
    """The 10 s timeout only fails closed if TimeoutExpired is caught — on a
    hung filesystem an uncaught one would propagate out of the gate."""

    def hanging(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(subprocess, "run", hanging)
    assert paths.repo_root(str(tmp_path)) is None


def test_repo_root_requires_zero_rc_and_output(monkeypatch, tmp_path):
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="/repo\n", stderr="")

    monkeypatch.setattr(subprocess, "run", failing)
    assert paths.repo_root(str(tmp_path)) is None

    def silent(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", silent)
    assert paths.repo_root(str(tmp_path)) is None
