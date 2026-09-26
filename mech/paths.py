"""Repository- and project-root resolution.

This module used to carry the enforcement surface — `SURFACE`, `DENY_SURFACE`,
the glob matcher and `is_guarded` — for the edit/bash guards to consult. Those
guards were unregistered on 2026-07-25 when gate-config changes became a
convention rather than a mechanism, and deleted once nothing invoked them; the
surface machinery went with them. The prior posture is tagged
`full-enforcement-2026-07-25`.
"""

import os
import subprocess


def repo_root(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607  # PATH-resolved `git` is intentional; no fixed install path to trust instead
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def project_root(cwd: str, repository_root: str) -> str:
    """Resolve Pawl's project root within a possibly larger Git repository.

    Pawl's project-owned configuration lives beside ``pyproject.toml``. Walk
    from the invocation directory toward the Git root so a nested deployment
    selects its own project, while an invocation from a normal repository
    subdirectory still selects the repository root. If the invocation is
    outside the repository or no project marker is present, fail safely back
    to the Git root.
    """
    current = os.path.realpath(cwd)
    repository = os.path.realpath(repository_root)
    try:
        if os.path.commonpath((current, repository)) != repository:
            return repository
    except ValueError:  # Different drives on Windows cannot share a path.
        return repository

    while True:
        if os.path.isfile(os.path.join(current, "pyproject.toml")):
            return current
        if current == repository:
            return repository
        parent = os.path.dirname(current)
        if parent == current:
            return repository
        current = parent
