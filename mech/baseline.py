"""MUTANTS.md survivor baseline (spec §7, REQ-B-1).

Cited REQ-G-6 until 2026-07-26, which was the edit guard's parsed-table rule
for MUTANTS.md edits. That requirement is withdrawn (spec 2026-07-25 amendment)
and its implementation here, `allowed_edit`, is deleted. What survives is the
format itself: REQ-B-1/B-1a/B-2 still describe the five-cell row, and
`parse` is live from `ci_weekly`.
"""

import re

from mech import mutation

HEADER = (
    "| mutant ID | class | reason | guard-or-reopen-condition | mutation fingerprint |"
)
DIVIDER = "|---|---|---|---|---|"
CLASSES = ("equivalent", "environment-equivalent", "guarded-elsewhere")

Row = tuple[str, str, str, str, str]


class BaselineError(ValueError):
    pass


def parse(text: str) -> list[Row]:
    """REQ-B-1 total parser: \\n-split, one trailing \\r stripped, blank lines
    ignored, header+divider byte-exact, markdown pipe-form segment dropping.
    A pipe preceded by a backslash is cell content, not a separator, and
    unescapes to a literal `|` — markdown's own convention, and `render`'s
    inverse (a reason cell legitimately contains e.g. `str | None`)."""
    raw = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    lines = [line for line in raw if line.strip()]
    if len(lines) < 2 or lines[0] != HEADER or lines[1] != DIVIDER:
        raise BaselineError("missing or wrong header")
    rows: list[Row] = []
    seen: set[str] = set()
    for line in lines[2:]:
        segments = re.split(r"(?<!\\)\|", line)
        if segments and not segments[0].strip():
            segments = segments[1:]
        if segments and not segments[-1].strip():
            segments = segments[:-1]
        cells = [c.strip().replace("\\|", "|") for c in segments]
        if len(cells) != 5:
            raise BaselineError(f"row must have exactly 5 cells: {line!r}")
        if cells[0] in seen:
            raise BaselineError(f"duplicate mutant ID: {cells[0]}")
        seen.add(cells[0])
        rows.append((cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows


def render(rows: list[Row]) -> str:
    """Inverse of `parse`: a `|` inside a cell is written `\\|` so the row
    stays one markdown table cell and round-trips."""
    body = [HEADER, DIVIDER]
    body += [
        "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
        for row in rows
    ]
    return "\n".join(body) + "\n"


def validate_row(row: Row) -> "str | None":
    """Validate one one-line row over any other cell characters."""
    if any("\n" in cell or "\r" in cell for cell in row):
        return "cells must stay on one line (carriage returns and newlines are invalid)"
    mid, cls, reason, cond, fingerprint = row
    if not mid:
        return "empty mutant ID"
    if cls not in CLASSES:
        return f"unknown class: {cls!r}"
    if not reason:
        return "empty reason"
    if cls == "equivalent" and cond:
        return "equivalent rows must leave the fourth cell empty"
    if cls != "equivalent" and not cond:
        return f"{cls} rows require a fourth cell"
    if cls == "guarded-elsewhere" and not re.fullmatch(
        r"ruff:[A-Za-z][A-Za-z0-9_-]*", cond
    ):
        return "guarded-elsewhere delegate must be ruff:<CODE> (a single rule code)"
    # Grammar only. Whether the hash MATCHES the mutation at this ID is a
    # binding check needing the generated tree — no predicate over these five
    # cells can see it (REQ-B-4).
    if not mutation.FINGERPRINT_RE.match(fingerprint):
        return "mutation fingerprint must be 12 lowercase hex digits"
    return None
