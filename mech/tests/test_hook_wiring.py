"""Which git operations actually run a gate hook, and whether a red hook stops them.

Every other test drives the gate through scripted `run` callables, so nothing
exercised the contract that decides whether a commit is gated at all: that git
fires *our* hook for the operation the user performed. That blind spot is what
let clean merge commits go ungated while `mech.validation` still called the
clone armed (2026-07-27).

These tests deliberately do NOT invoke `mech.commit_gate` — it runs pytest, so
a test that ran the real gate would recurse into this suite. The gate's own
behaviour is covered in test_gate.py. What is covered here is the wiring
underneath it: operation -> hook -> exit status -> commit created or not.
Recording stand-ins named for each `GATE_HOOKS` entry stand in for the gate, so
a failure here means the git-side mapping changed, not that a check regressed.
"""

import os
import subprocess

import pytest

from mech import validation


def _git(*args, cwd, check=True):
    # `git` is PATH-resolved intentionally and every arg is a literal from this
    # file, never untrusted input.
    return subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["git", *args],  # noqa: S607  # PATH-resolved `git` is intentional
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _arm(repo, log, exit_code=0):
    """Install a recording stand-in for every hook pawl ships."""
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    for name in validation.GATE_HOOKS:
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\necho {name} >> "{log}"\nexit {exit_code}\n')
        hook.chmod(0o755)
    _git("config", "core.hooksPath", ".githooks", cwd=repo)


def _repo(tmp_path):
    """An UNARMED repo with one base commit.

    Arming is a separate, explicit step that each test performs immediately
    before its trigger. Doing it here instead meant setup commits ran the hooks
    too, and their entries in the log read exactly like a finding.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "base.txt").write_text("base\n")
    _git("add", "base.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "base", cwd=tmp_path)
    return tmp_path


def _ran(log):
    return log.read_text().split() if log.exists() else []


def _head(repo):
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _make_mergeable_branch(repo):
    """A side branch touching a different file, so the merge is clean."""
    _git("checkout", "-q", "-b", "side", cwd=repo)
    (repo / "side.txt").write_text("side\n")
    _git("add", "side.txt", cwd=repo)
    _git("commit", "-q", "-m", "side", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)


def test_plain_commit_runs_pre_commit(tmp_path):
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    (repo / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=repo)
    _arm(repo, log)
    _git("commit", "-m", "a", cwd=repo)
    assert _ran(log) == ["pre-commit"]


def test_amend_runs_pre_commit(tmp_path):
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    _arm(repo, log)
    _git("commit", "-q", "--amend", "-m", "amended", cwd=repo)
    assert _ran(log) == ["pre-commit"]


def test_clean_auto_merge_runs_pre_merge_commit(tmp_path):
    """The regression this file exists for. A clean auto-merge never touches
    pre-commit, so shipping only that hook left merge commits uncertified."""
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    _make_mergeable_branch(repo)
    _arm(repo, log)
    before = _head(repo)
    _git("merge", "--no-ff", "-m", "merge side", "side", cwd=repo)
    assert _ran(log) == ["pre-merge-commit"]
    assert _head(repo) != before  # a merge commit really was created


def test_red_pre_commit_blocks_the_commit(tmp_path):
    """Fail-closed at the git boundary: the hook's exit status is the whole
    mechanism by which a failing gate stops a commit."""
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    before = _head(repo)
    (repo / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=repo)
    _arm(repo, log, exit_code=1)
    result = _git("commit", "-m", "a", cwd=repo, check=False)
    assert result.returncode != 0
    assert _head(repo) == before


def test_red_pre_merge_commit_blocks_the_merge(tmp_path):
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    _make_mergeable_branch(repo)
    _arm(repo, log, exit_code=1)
    before = _head(repo)
    result = _git("merge", "--no-ff", "-m", "merge side", "side", cwd=repo, check=False)
    assert result.returncode != 0
    assert _head(repo) == before


@pytest.mark.parametrize("operation", ["cherry-pick", "revert"])
def test_known_ungated_operations_run_no_hook(tmp_path, operation):
    """Tripwire for BACKLOG 8, not an endorsement. git runs no
    pre-commit-equivalent for these, so they create commits the gate never sees
    and push CI is their only check.

    If this test starts FAILING, git gained a hook for the operation and the gap
    can be closed — revisit BACKLOG 8 rather than deleting the assertion.
    """
    log = tmp_path / "hooks.log"
    repo = _repo(tmp_path / "r")
    _make_mergeable_branch(repo)
    _arm(repo, log)
    before = _head(repo)
    if operation == "cherry-pick":
        _git("cherry-pick", "side", cwd=repo)
    else:
        _git("revert", "--no-edit", "HEAD", cwd=repo)
    assert _head(repo) != before  # the operation really did create a commit
    assert _ran(log) == []


@pytest.mark.parametrize("hook", validation.GATE_HOOKS)
def test_hook_aborts_when_the_repo_root_cannot_be_resolved(tmp_path, hook):
    """`cd "$(git rev-parse --show-toplevel)" || exit 1` reads as a guard but
    cannot trip: a failed substitution is the empty string and `cd ""` returns 0
    in /bin/sh, so the gate would run from whatever directory git left behind.

    Stub `git` and `uv` on PATH rather than the real ones: if the guard fails to
    trip, the stub records that the gate WOULD have run, instead of this test
    invoking the real gate and recursing into its own pytest.
    """
    root = validation.paths.repo_root(os.getcwd()) or "."
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexit 128\n")  # rev-parse always fails
    marker = tmp_path / "gate_ran"
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho ran > "{marker}"\nexit 0\n')
    for stub in ("git", "uv"):
        (bin_dir / stub).chmod(0o755)

    result = subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["/bin/sh", os.path.join(root, ".githooks", hook)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0, f"{hook} continued after rev-parse failed"
    assert not marker.exists(), f"{hook} ran the gate from an unknown directory"


@pytest.mark.parametrize("hook", validation.GATE_HOOKS)
def test_hook_explains_itself_when_uv_is_missing_from_path(tmp_path, hook):
    """Desktop git clients run hooks with the login PATH, where uv usually is
    not. The posture was already correct — exec fails, the commit is blocked —
    but the only output was `exec: uv: not found`, which reads as a broken repo
    rather than as unset PATH.

    PATH here holds git and nothing else, so uv genuinely cannot be found and
    the real gate cannot run.
    """
    root = validation.paths.repo_root(os.getcwd()) or "."
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_path = subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["/usr/bin/which", "git"], capture_output=True, text=True, check=True
    ).stdout.strip()
    os.symlink(git_path, bin_dir / "git")

    result = subprocess.run(  # noqa: S603  # fixed literal args, not untrusted input
        ["/bin/sh", os.path.join(root, ".githooks", hook)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": str(bin_dir)},
    )
    assert result.returncode != 0, f"{hook} proceeded without uv"
    assert "PATH" in result.stderr, f"{hook} did not explain why: {result.stderr!r}"
