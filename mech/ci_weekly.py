"""Weekly-CI entry: full mutmut, retire candidates, pip-audit, issue handoff
(REQ-CI-3).

Unlike `ci_push.main`, this does not short-circuit: a step failure (mutmut,
pip-audit, gh query) becomes a handoff action item in `delta["errors"]` so
that later steps whose inputs remain valid still run (REQ-CI-3 "errors are
handoff items too"). Every tool subprocess invocation's argv/timeout comes
from the `tools.CI` table (REQ-ARCH-9) via the `run=` seam.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from mech import baseline, mutation, tools

_CI: dict[str, tuple[tuple[str, ...], int]] = {
    slot: (argv, timeout) for slot, argv, timeout in tools.CI
}


_scrubbed_env = tools.scrubbed_env


def build_delta(full_states: dict, rows, actual_fingerprints: dict) -> dict:
    """REQ-CI-3 (a)/(b)/(ca)/(cb)/(c): every baseline row the full run generated
    but did not kill is settled here.

    A generated, still-surviving row whose fingerprint no longer matches falls
    into none of the others — it is in the baseline, it was generated, and it
    survived — so without (ca) it keeps suppressing a mutation nobody described
    (REQ-B-3 case c).

    A killed `environment-equivalent` row is NOT retire-as-satisfied. This job
    runs on the CI host; those rows claim only that their mutant is unkillable on
    the *measurement* host, and state the condition under which it dies, so a kill
    here confirms the row rather than obsoleting it. Verified 2026-07-25 (run
    30148777473): 19 such rows were killed on ext4 while still surviving on the
    macOS measurement host, so retiring them would have removed suppressions that
    host still needs — the local gate would block on them, the honest
    classification would be `environment-equivalent` again, and the row would
    oscillate. What this cannot distinguish is a row killed by its own stated
    condition from one killed for an unrelated reason; both present as a CI-host
    kill, so `host_divergent` is a human read, aided by the delta the REQ-B-4
    report prints.
    """
    generated = {mid for ids in full_states.values() for mid in ids}
    survived = set()
    for state in mutation.SURVIVOR_STATES:
        survived.update(full_states.get(state, []))
    # Only a detected state (PASSING_STATES: killed/timeout) is a kill. "Not
    # survived" also covers `no tests`, `not checked`, `skipped` and crash
    # states — a mutant nothing ever exercised — and counting those as killed
    # told the operator to retire rows the run never verified (worklist item
    # 4, 2026-08-02). Those land in `inconclusive` instead.
    detected = set()
    for state in mutation.PASSING_STATES:
        detected.update(full_states.get(state, []))
    baseline_ids = {r[0] for r in rows}
    class_of = {r[0]: r[1] for r in rows}
    # A contradictory catalog (one ID both survived and detected) resolves in
    # the survivor's favour, as it always has — survival is the claim a
    # baseline row exists to suppress.
    killed = {m for m in baseline_ids if m in detected and m not in survived}
    return {
        "new_survivors": sorted(s for s in survived if s not in baseline_ids),
        "retire_satisfied": sorted(
            m for m in killed if class_of[m] != "environment-equivalent"
        ),
        "host_divergent": sorted(
            m for m in killed if class_of[m] == "environment-equivalent"
        ),
        "retire_obsolete": sorted(m for m in baseline_ids if m not in generated),
        "inconclusive": sorted(
            m
            for m in baseline_ids
            if m in generated and m not in survived and m not in detected
        ),
        # `in generated` guard: an ungenerated row has no fingerprint to compare,
        # so without it every retire-obsolete row would double-report here.
        "fingerprint_mismatch": sorted(
            r[0]
            for r in rows
            if r[0] in generated and actual_fingerprints.get(r[0]) != r[4]
        ),
        "errors": [],  # step errors are appended by main() as handoff action items
    }


def job_red(delta: dict) -> bool:
    """REQ-CI-3: red on new survivors, a fingerprint mismatch, an inconclusive
    baseline row, any pip-audit finding, or any step error; a delta of retire
    candidates only stays green. Inconclusive is red because verifying the
    baseline is this job's one purpose — a row it could not verify is a failed
    verification, not housekeeping."""
    return (
        bool(delta["new_survivors"])
        or bool(delta.get("fingerprint_mismatch"))
        or bool(delta.get("inconclusive"))
        or bool(delta.get("pip_audit"))
        or bool(delta.get("errors"))
    )


def already_posted(open_issues: list, run_id: str) -> bool:
    """REQ-CI-3 idempotency: search canonical issue body + all comments for
    the exact `run-id: <id>` line (whole line, trailing whitespace stripped)
    before any create/comment — a substring match would false-positive on a
    prefix collision (e.g. run "4" inside a body line "run-id: 41")."""
    needle = f"run-id: {run_id}"
    for issue in open_issues:
        texts = [issue.get("body") or ""]
        texts += [c.get("body") or "" for c in issue.get("comments") or []]
        for text in texts:
            if any(line.rstrip() == needle for line in text.splitlines()):
                return True
    return False


def canonical_issues(open_issues: list) -> list:
    """REQ-CI-3: canonical = open, label-exact `mech-weekly` — title plays no
    part in selection. Defensive re-filter on top of the (already
    label+state filtered) `gh issue list` query. No PR check: `gh issue
    list` never returns pull requests (confirmed live against gh 2.94.0 on
    2026-07-23 — `isPullRequest` isn't even a valid --json field for that
    subcommand), so filtering for one here would be dead code."""
    result = []
    for issue in open_issues:
        labels = issue.get("labels")
        if labels is not None:
            names = {
                label["name"] if isinstance(label, dict) else label for label in labels
            }
            if "mech-weekly" not in names:
                continue
        result.append(issue)
    return result


def issue_plan(open_issues: list, delta_empty: bool) -> str:
    if delta_empty:
        return "none"
    if len(open_issues) == 0:
        return "create"
    if len(open_issues) == 1:
        return "comment"
    return "fail_duplicates"


def _body(run_id: str, delta: dict) -> str:
    return (
        f"run-id: {run_id}\n\n"
        f"## new unclassified survivors\n{delta['new_survivors']}\n\n"
        f"## retire-as-satisfied candidates\n{delta['retire_satisfied']}\n\n"
        f"## retire-as-obsolete candidates\n{delta['retire_obsolete']}\n\n"
        f"## host-divergent rows (killed on CI host, condition confirmed)\n"
        f"{delta.get('host_divergent', [])}\n\n"
        f"## inconclusive rows (generated, neither killed nor surviving — "
        f"verify by hand)\n{delta.get('inconclusive', [])}\n\n"
        f"## fingerprint mismatches (retire and re-add)\n"
        f"{delta.get('fingerprint_mismatch', [])}\n\n"
        f"## step errors (action items)\n{delta.get('errors', [])}\n\n"
        f"## pip-audit\n{[v.get('name') for v in delta.get('pip_audit', [])]}\n"
    )


def main(root: "str | None" = None, run=subprocess.run) -> int:
    root = root or os.getcwd()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    errors: list[str] = []
    states: dict = {}
    results_ok = False
    mutation_config_error = None
    if os.path.isfile(os.path.join(root, "pyproject.toml")):
        mutation_config_error = mutation.xdist_config_error(root)
        if mutation_config_error is not None:
            errors.append(mutation_config_error)

    if mutation_config_error is None:
        mutation_env = mutation.execution_env()
        run_argv, run_timeout = _CI["ci-mutmut-run-full"]
        try:
            proc = run(
                list(run_argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=run_timeout,
                check=False,
                env=mutation_env,
            )
            if proc.returncode not in (0, 2):
                errors.append(f"mutmut full run failed: exit {proc.returncode}")
            else:
                results_argv = tools.MUTMUT_RESULTS_ALL
                results_timeout = tools.MUTMUT_RESULTS_TIMEOUT
                results = run(
                    list(results_argv),
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=results_timeout,
                    check=False,
                    env=mutation_env,
                )
                if results.returncode != 0:
                    errors.append(f"mutmut results failed: exit {results.returncode}")
                else:
                    states = mutation.parse_results(results.stdout)
                    if mutation.UNPARSEABLE_STATE in states:
                        # Garbled output that still exits 0 is a failed step, not a
                        # catalog (REQ-CG-4's fail-closed rule, applied here).
                        errors.append(
                            "mutmut results unparseable: "
                            f"{states[mutation.UNPARSEABLE_STATE][0]!r}"
                        )
                        states = {}
                    else:
                        results_ok = True
        except Exception as exc:  # noqa: BLE001  # error is a handoff item
            errors.append(f"mutmut step errored: {type(exc).__name__}: {exc}")

    baseline_ok = True
    try:
        with open(os.path.join(root, "MUTANTS.md"), encoding="utf-8") as fh:
            rows = baseline.parse(fh.read())
    except (OSError, baseline.BaselineError) as exc:
        errors.append(f"MUTANTS.md unreadable: {type(exc).__name__}: {exc}")
        rows = []
        baseline_ok = False
    # `parse` is total by contract (REQ-B-1); the schema lives in validate_row,
    # and this is the read path, so an invalid hand-edit must redden the job
    # here or nowhere. Any invalid row makes the catalog non-authoritative,
    # so no survivor or retirement advice is derived from it.
    for row in rows:
        problem = baseline.validate_row(row)
        if problem is not None:
            errors.append(f"MUTANTS.md invalid row {row[0]}: {problem}")
            baseline_ok = False

    # The error map needs no separate channel: an unresolvable ID is either not
    # generated (already retire-obsolete) or generated with no usable fingerprint,
    # which fails the comparison below and lands in fingerprint_mismatch.
    actual, _ = mutation.fingerprints([r[0] for r in rows], root)
    # A failed or unparseable mutmut phase yields no catalog to reconcile
    # against. Reconciling the baseline with an empty catalog would list every
    # row as retire-obsolete — data telling the operator to delete a correct
    # baseline, right beside the error saying the run produced nothing — so
    # the baseline sits the reconciliation out and only the errors report.
    comparable = results_ok and baseline_ok
    delta = build_delta(
        states if comparable else {}, rows if comparable else [], actual
    )

    vulns: list = []
    try:
        export_argv, export_timeout = _CI["ci-uv-export"]
        export_res = run(
            list(export_argv),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=export_timeout,
            check=False,
            env=_scrubbed_env(),
        )
        if export_res.returncode != 0:
            errors.append(f"uv export failed: exit {export_res.returncode}")
        else:
            os.makedirs(os.path.join(root, ".mech"), exist_ok=True)
            with open(
                os.path.join(root, ".mech", "requirements.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(
                    export_res.stdout
                )  # REQ-CI-3: audits the exported lock, never ambient
            audit_argv, audit_timeout = _CI["ci-pip-audit"]
            audit = run(
                list(audit_argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=audit_timeout,
                check=False,
                env=_scrubbed_env(),
            )
            # REQ-CI-3d (E2): pip-audit 2.10.1 exits 1 both for findings and
            # for a crash (probed: missing -r file → rc 1, empty stdout), so
            # a crash is any unknown exit code OR an empty stdout — never
            # silently zero findings.
            if audit.returncode not in (0, 1) or not audit.stdout.strip():
                errors.append(f"pip-audit failed: exit {audit.returncode}")
            else:
                audit_data = json.loads(audit.stdout)
                vulns = [
                    d for d in audit_data.get("dependencies", []) if d.get("vulns")
                ]
    except Exception as exc:  # noqa: BLE001  # a step error is a handoff item
        errors.append(f"pip-audit step errored: {type(exc).__name__}: {exc}")

    # The listing runs before `delta["errors"]` freezes so its failure lands
    # there (job_red reddens any step error). `None` is could-not-determine —
    # distinct from [] ("none open") because a failed listing must not take
    # the create path: filing without knowing what is open is exactly the
    # duplicate-issue bug (panel 2026-08-02, item 9).
    raw_issues: list | None = []
    try:
        listing_argv, listing_timeout = _CI["ci-gh-issue-list"]
        listing = run(
            list(listing_argv),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=listing_timeout,
            check=False,
            env=_scrubbed_env(),
        )
        if listing.returncode == 0:
            raw_issues = json.loads(listing.stdout or "[]")
        else:
            raw_issues = None
            errors.append(f"gh issue list failed: exit {listing.returncode}")
    except Exception as exc:  # noqa: BLE001  # a step error is a handoff item
        raw_issues = None
        errors.append(f"gh issue list errored: {type(exc).__name__}: {exc}")

    delta["pip_audit"] = vulns
    delta["errors"] = errors
    # Every bucket is reportable, so ask the delta rather than an allow-list of
    # keys. That list silently omitted fingerprint_mismatch and then
    # host_divergent as each was added — run 30153755743 went green and filed
    # nothing while holding 19 host-divergent rows, and a mismatch-only run would
    # have gone red with no handoff at all, which REQ-CI-3 forbids ("the red
    # badge alone is never the handoff"). All values are lists; a future non-list
    # bucket needs its own emptiness rule.
    delta_empty = not any(delta.values())
    body = _body(run_id, delta)

    canonical = canonical_issues(raw_issues) if raw_issues is not None else None

    if canonical is not None and not already_posted(canonical, run_id):
        # requery-compare-then-act (REQ-CI-3): `canonical` above is the
        # re-query, taken immediately before this decision.
        plan = issue_plan(canonical, delta_empty)
        if plan == "create":
            label_argv, label_timeout = _CI["ci-gh-label-create"]
            run(
                list(label_argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=label_timeout,
                check=False,
                env=_scrubbed_env(),
            )
            date = datetime.now(UTC).strftime("%Y-%m-%d")
            create_argv_t, create_timeout = _CI["ci-gh-issue-create"]
            create_argv = [
                a.replace("{title}", f"mech weekly: {date}").replace("{body}", body)
                for a in create_argv_t
            ]
            created = run(
                create_argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=create_timeout,
                check=False,
                env=_scrubbed_env(),
            )
            if created.returncode != 0:
                print("issue creation failed", file=sys.stderr)
                return 1
        elif plan == "comment":
            comment_argv_t, comment_timeout = _CI["ci-gh-issue-comment"]
            comment_argv = [
                a.replace("{number}", str(canonical[0]["number"])).replace(
                    "{body}", body
                )
                for a in comment_argv_t
            ]
            commented = run(
                comment_argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=comment_timeout,
                check=False,
                env=_scrubbed_env(),
            )
            if commented.returncode != 0:
                print("issue comment failed", file=sys.stderr)
                return 1
        elif plan == "fail_duplicates":
            print(
                "duplicate mech-weekly issues open — resolve manually", file=sys.stderr
            )
            return 1

    for error in errors:
        print(f"ci_weekly step error: {error}", file=sys.stderr)
    return 1 if job_red(delta) else 0


if __name__ == "__main__":
    raise SystemExit(main())
