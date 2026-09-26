"""Stop-hook semantics (spec REQ-T2-1..T2-3): report, never block on red tests."""

import re

SEED_RE = re.compile(r"Using --randomly-seed=(\d+)")
FAILED_RE = re.compile(r"^(FAILED|ERROR) (\S+)", re.MULTILINE)
COUNT_RE = re.compile(r"(\d+ (?:failed|error)[^\n]*)")
# Stripped before any matching below. FORCE_COLOR/PY_COLORS in the launching
# shell make pytest colour its summary even into a pipe, and FAILED_RE anchors
# at line start — so an SGR prefix made a red suite report `0 failed/errored`
# out loud (2026-07-27). Stripping here rather than passing `--color=no` fixes
# it whatever the source: env var, addopts, or a plugin that colours anyway.
# Parameter bytes are the full ECMA-48 0x30-0x3F range, not just digits and
# `;`: ITU-T T.416 SGR may separate parameters with `:` (\x1b[38:5:196m), and
# some tools emit that form (2026-08-02).
ANSI_RE = re.compile(r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]")


def _seed_line(output: str) -> str:
    match = SEED_RE.search(output)
    return f"Using --randomly-seed={match.group(1)}" if match else "seed unavailable"


def summarize(
    exit_code: int, output: str, stop_hook_active: bool
) -> tuple[str, str | None]:
    # Colour is a rendering detail and must never change the verdict; the tail
    # below is pasted into a message a human reads, so escapes go here too.
    output = ANSI_RE.sub("", output)
    if exit_code == 0:
        return "silent", None
    if exit_code == 1:
        # REQ-T2-2 extraction contract: count = number of FAILED/ERROR
        # short-summary lines; first 5 in order of appearance
        matches = [m.group(2) for m in FAILED_RE.finditer(output)]
        return "message", (
            f"pytest: {len(matches)} failed/errored | "
            f"first failures: {', '.join(matches[:5]) or 'n/a'} | "
            f"{_seed_line(output)}"
        )
    if exit_code == 5:
        return "message", "pytest: no tests collected"
    tail = output[-600:]
    message = (
        f"pytest infrastructure red (exit {exit_code}): {tail} | {_seed_line(output)}"
    )
    if stop_hook_active:
        return "message", message
    return "block", message
