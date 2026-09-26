"""Explicit Pawl self-test and boundary-verification record.

Deployments keep ordinary pytest product-owned.  Adoption, sync, and any
change to the declared compatibility inputs run this module with `--record`;
the ordinary commit gate checks the resulting digest without rerunning Pawl's
suite.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable

from mech import paths, tools

SCHEMA = 1
SELFTEST_ARGV = (
    "uv",
    "run",
    "pytest",
    "mech/tests",
    "-q",
    tools.REQUIRED_PYTEST_SEED_ARG,
)
COMPATIBILITY_FILES = (
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "pyrightconfig.json",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "conftest.py",
    ".codex/hooks.json",
)
COMPATIBILITY_DIRS = ("mech", ".githooks", ".github/workflows")
REPOSITORY_COMPATIBILITY_FILES = frozenset({".codex/hooks.json"})
REPOSITORY_COMPATIBILITY_DIRS = frozenset({".githooks", ".github/workflows"})
IGNORED_PARTS = {"__pycache__", ".DS_Store"}
RECORD_PATH_ERROR = (
    "pawl_selftest_record must be a nonempty relative path inside the repository"
)
Run = Callable[..., "subprocess.CompletedProcess"]


class RecordConfigError(ValueError):
    """The deployment opted into a record but configured it unsafely."""


def configured_record(pyproject_text: str) -> "str | None":
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise RecordConfigError(f"pyproject.toml is unparseable: {exc}") from exc
    tool = data.get("tool")
    mech = tool.get("mech") if isinstance(tool, dict) else None
    if not isinstance(mech, dict) or "pawl_selftest_record" not in mech:
        return None
    value = mech["pawl_selftest_record"]
    if not isinstance(value, str) or not value or os.path.isabs(value):
        raise RecordConfigError(RECORD_PATH_ERROR)
    normalised = value.replace("\\", "/")
    if ".." in normalised.split("/"):
        raise RecordConfigError(RECORD_PATH_ERROR)
    return normalised


def _digest_file(digest: "hashlib._Hash", root: str, rel_path: str) -> None:
    digest.update(rel_path.encode("utf-8"))
    digest.update(b"\0")
    full = os.path.join(root, *rel_path.split("/"))
    if not os.path.isfile(full):
        digest.update(b"<absent>\0")
        return
    with open(full, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    digest.update(b"\0")


def compatibility_digest(root: str) -> str:
    repository_root = paths.repo_root(root) or root
    digest = hashlib.sha256()
    for rel_path in COMPATIBILITY_FILES:
        base = repository_root if rel_path in REPOSITORY_COMPATIBILITY_FILES else root
        _digest_file(digest, base, rel_path)
    for rel_dir in COMPATIBILITY_DIRS:
        base = repository_root if rel_dir in REPOSITORY_COMPATIBILITY_DIRS else root
        full_dir = os.path.join(base, *rel_dir.split("/"))
        digest.update(f"{rel_dir}/".encode())
        digest.update(b"\0")
        if not os.path.isdir(full_dir):
            digest.update(b"<absent>\0")
            continue
        for dirpath, dirnames, filenames in os.walk(full_dir):
            dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_PARTS)
            for filename in sorted(filenames):
                if filename in IGNORED_PARTS or filename.endswith((".pyc", ".pyo")):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, filename), base)
                _digest_file(digest, base, rel.replace(os.sep, "/"))
    return digest.hexdigest()


def _record_path(root: str, record: str) -> str:
    return os.path.join(root, *record.split("/"))


def record_error(root: str, record: str) -> "str | None":
    path = _record_path(root, record)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return f"Pawl self-test record is missing: {record}"
    except (OSError, ValueError) as exc:
        return f"Pawl self-test record is unreadable: {type(exc).__name__}: {exc}"
    if data.get("schema") != SCHEMA or not isinstance(data.get("digest"), str):
        return f"Pawl self-test record has unsupported schema: {record}"
    if data["digest"] != compatibility_digest(root):
        return (
            f"Pawl self-test record is stale: {record} — run "
            "`uv run python -m mech.selftest --record`"
        )
    return None


def write_record(root: str, record: str) -> None:
    path = _record_path(root, record)
    parent = os.path.dirname(path) or root
    os.makedirs(parent, exist_ok=True)
    payload = json.dumps(
        {"schema": SCHEMA, "digest": compatibility_digest(root)},
        sort_keys=True,
        separators=(",", ":"),
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=parent, delete=False
    ) as fh:
        temp_path = fh.name
        fh.write(payload + "\n")
    os.replace(temp_path, path)


def run_selftest(root: str, run: Run = subprocess.run) -> int:
    try:
        proc = run(
            list(SELFTEST_ARGV),
            cwd=root,
            check=False,
            env=tools.scrubbed_env(),
        )
    except Exception as exc:  # noqa: BLE001  # explicit boundary fails closed
        print(
            f"Pawl self-test could not run: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    if proc.returncode == 4:
        print(
            "Pawl self-test could not collect mech/tests (pytest exit 4)",
            file=sys.stderr,
        )
        return 1
    if proc.returncode == 5:
        print(
            "Pawl self-test collected no tests from mech/tests (pytest exit 5)",
            file=sys.stderr,
        )
        return 1
    if proc.returncode != 0:
        print(f"Pawl self-test failed with exit {proc.returncode}", file=sys.stderr)
        return 1
    return 0


def _read_pyproject(root: str) -> str:
    try:
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def check_record(root: str) -> "str | None":
    try:
        record = configured_record(_read_pyproject(root))
    except RecordConfigError as exc:
        return f"Pawl self-test configuration invalid: {exc}"
    return record_error(root, record) if record is not None else None


def run_and_record(root: str, run: Run = subprocess.run) -> int:
    try:
        record = configured_record(_read_pyproject(root))
    except RecordConfigError as exc:
        print(f"Pawl self-test configuration invalid: {exc}", file=sys.stderr)
        return 1
    if record is None:
        print(
            "Pawl self-test recording is not configured at "
            "[tool.mech].pawl_selftest_record",
            file=sys.stderr,
        )
        return 1
    result = run_selftest(root, run=run)
    if result != 0:
        return result
    write_record(root, record)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    cwd = os.getcwd()
    repository_root = paths.repo_root(cwd)
    root = (
        paths.project_root(cwd, repository_root) if repository_root is not None else cwd
    )
    if args.check:
        error = check_record(root)
        if error is not None:
            print(error, file=sys.stderr)
            return 1
        return 0
    if args.record:
        return run_and_record(root)
    return run_selftest(root)


if __name__ == "__main__":
    raise SystemExit(main())
