"""Verdict constructors — the REQ-ARCH-6 schema for gate-facing functions."""

from typing import Any

PASS = "pass"  # noqa: S105  # verdict outcome constant, not a secret
FAIL = "fail"  # noqa: S105  # verdict outcome constant, not a secret
ERROR = "error"  # noqa: S105  # verdict outcome constant, not a secret


def finding(
    tool: str,
    message: str,
    code: str | None = None,
    path: str | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    return {"tool": tool, "code": code, "path": path, "line": line, "message": message}


def passed(summary: str | None = None) -> dict[str, Any]:
    return {"outcome": PASS, "findings": [], "summary": summary}


def failed(
    findings: list[dict[str, Any]], summary: str | None = None
) -> dict[str, Any]:
    return {"outcome": FAIL, "findings": findings, "summary": summary}


def errored(message: str, tool: str = "mech") -> dict[str, Any]:
    return {"outcome": ERROR, "findings": [finding(tool, message)], "summary": message}
