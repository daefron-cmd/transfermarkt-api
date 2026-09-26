"""Commit-gate entry point, invoked by .githooks/pre-commit. Fail-closed."""

import os
import sys

from mech import gate, paths


def main() -> int:
    cwd = os.getcwd()
    repository_root = paths.repo_root(cwd)
    if repository_root is None:
        print("commit gate: no git repo root — refusing to certify", file=sys.stderr)
        return 1
    root = paths.project_root(cwd, repository_root)
    try:
        result = gate.run_gate(root)
    except Exception as exc:  # noqa: BLE001  # fail-closed (REQ-CG-5)
        print(f"commit gate errored: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Human escape hatch (mech bug blocking its own fix): "
            "git commit --no-verify",
            file=sys.stderr,
        )
        return 1
    if result["outcome"] != "pass":
        for finding in result["findings"]:
            print(f"[{finding['tool']}] {finding['message']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
