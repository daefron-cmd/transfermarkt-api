"""Stop entry point: full suite, report-only (spec §6.1). Fail-open."""

import subprocess
import sys

from mech import hookio, stoprun, tools


def main(run=subprocess.run) -> int:
    try:
        event = hookio.read_event(sys.stdin)
        # absent stop_hook_active → True: never block (REQ-T2-3)
        active = bool(event.get("stop_hook_active", True))
        # REQ-T2-1: NO mech-imposed cutoff — only the 300s registration timeout
        # bounds this run.
        proc = run(
            ["uv", "run", "pytest"],
            capture_output=True,
            text=True,
            check=False,
            # Same scrub the commit gate applies: this hook is the only
            # always-on check, so ambient PYTEST_ADDOPTS must not neuter it.
            env=tools.scrubbed_env(),
        )
        action, message = stoprun.summarize(
            proc.returncode, proc.stdout + proc.stderr, active
        )
        if action == "block":
            return hookio.stop_block(message or "infrastructure red")
        if action == "message":
            return hookio.system_message(message or "")
        return 0
    except Exception:  # noqa: BLE001  # fail-open (spec §12 matrix)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
