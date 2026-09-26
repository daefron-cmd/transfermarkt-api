"""The deploy skills quote pawl's own config; these pin the quotes to reality.

The `pawl-adopt` and `pawl-sync` plugin skills tell a downstream repo what to copy,
so a stale quotation there is not a typo — it deploys the bug that was fixed
upstream. On 2026-07-28 a single day's work rotted four separate passages:
`arming.md` still carried the Stop command from before the cwd fix, so every new
deployment would have been born with it; `file-inventory.md` quoted a pre-commit
body missing both guards added hours earlier; `pawl-sync` never learned about
`pre-merge-commit`; and five places still said `uv sync --frozen`.

All four were found by reading. These tests cover the mechanically checkable
ones, so the next such change fails the gate rather than waiting to be noticed.
Prose cannot be pinned this way — that part still needs a human.
"""

import json
import os
import re
import subprocess
import tomllib

import pytest

from mech import paths, validation

SKILLS = ("pawl-adopt", "pawl-sync")
PLUGIN_ROOT = ("plugins", "pawl")
PLUGIN_SKILLS_ROOT = (*PLUGIN_ROOT, "skills")
STRICT_PAWL_MODULES = [
    "mech/baseline.py",
    "mech/imports_analyzer.py",
    "mech/source_inventory.py",
    "mech/stoprun.py",
]


def _root() -> str:
    return paths.repo_root(os.getcwd()) or "."


# This whole file is about pawl-the-project, not mech-the-stack. It travels
# inside `mech/tests/`, while `plugins/pawl/` does not. Without this guard its tests
# fail in every adopting repo — measured
# on a real brownfield adoption the day they were written. Keyed on
# the packaged `pawl-adopt` skill rather than bare `plugins/`: an adopting repo
# can have its own plugins, which would not skip the guard, and then every test
# in this file fails on missing Pawl plugin paths — a red gate on code the
# adopter did not write. Pawl itself always has the packaged skill, so the guard
# never fires here and cannot mask rot.
pytestmark = pytest.mark.skipif(
    not os.path.isdir(
        os.path.join(
            paths.repo_root(os.getcwd()) or ".", *PLUGIN_SKILLS_ROOT, "pawl-adopt"
        )
    ),
    reason="no packaged pawl-adopt skill — this is a deployment, not the pawl repo",
)


def _read(*parts: str) -> str:
    with open(os.path.join(_root(), *parts), encoding="utf-8") as fh:
        return fh.read()


def _read_skill(skill: str, *parts: str) -> str:
    return _read(*PLUGIN_SKILLS_ROOT, skill, *parts)


def test_deploy_skills_are_packaged_as_an_installable_plugin():
    manifest = json.loads(_read(*PLUGIN_ROOT, ".codex-plugin", "plugin.json"))
    marketplace = json.loads(_read(".agents", "plugins", "marketplace.json"))

    assert not os.path.exists(os.path.join(_root(), "skills"))
    assert manifest["name"] == "pawl"
    assert manifest["skills"] == "./skills/"
    assert marketplace["name"] == "pawl"
    assert {
        "name": "pawl",
        "source": {"source": "local", "path": "./plugins/pawl"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
    } in marketplace["plugins"]

    for skill in SKILLS:
        skill_file = os.path.join(_root(), *PLUGIN_SKILLS_ROOT, skill, "SKILL.md")
        assert os.path.isfile(skill_file), f"plugin does not package {skill}"


def _skill_docs() -> list[tuple[str, str]]:
    found = []
    for skill in SKILLS:
        base = os.path.join(_root(), *PLUGIN_SKILLS_ROOT, skill)
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                if name.endswith(".md"):
                    full = os.path.join(dirpath, name)
                    with open(full, encoding="utf-8") as fh:
                        found.append((os.path.relpath(full, _root()), fh.read()))
    assert found, "no skill docs found — has the Pawl plugin moved?"
    return found


def _arming_json_blocks() -> list:
    """Every fenced json block in arming.md, parsed.

    The doc carries more than one on purpose — the stock registration and the
    empty block a waived deployment writes — so a test that grabs "the" json
    block silently checks whichever happens to come first.
    """
    doc = _read_skill("pawl-adopt", "references", "arming.md")
    blocks = re.findall(r"```json\n(\{.*?\n?\})\n```", doc, re.S)
    assert blocks, "arming.md no longer contains any json block"
    return [json.loads(b) for b in blocks]


def _inventory_ruff_table() -> dict:
    """The `[tool.ruff]` tree as the copy inventory ships it.

    `file-inventory.md` carries several toml blocks; only one defines
    `tool.ruff`, so a test that grabbed "the" toml block would silently check
    whichever came first.
    """
    doc = _read_skill("pawl-adopt", "references", "file-inventory.md")
    for block in re.findall(r"```toml\n(.*?)```", doc, re.S):
        try:
            parsed = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            continue
        if "ruff" in parsed.get("tool", {}):
            return parsed["tool"]["ruff"]
    raise AssertionError("no toml block in file-inventory.md defines [tool.ruff]")


def _inventory_mutmut_table() -> dict:
    """The `[tool.mutmut]` tree as the copy inventory ships it."""
    doc = _read_skill("pawl-adopt", "references", "file-inventory.md")
    for block in re.findall(r"```toml\n(.*?)```", doc, re.S):
        try:
            parsed = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            continue
        if "mutmut" in parsed.get("tool", {}):
            return parsed["tool"]["mutmut"]
    raise AssertionError("no toml block in file-inventory.md defines [tool.mutmut]")


def _inventory_pyright_config() -> dict:
    doc = _read_skill("pawl-adopt", "references", "file-inventory.md")
    for block in re.findall(r"```json\n(.*?)```", doc, re.S):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if parsed.get("typeCheckingMode") == "standard":
            return parsed
    raise AssertionError("no standard pyright config in file-inventory.md")


def test_inventory_does_not_ship_mech_as_the_mutation_scope():
    """The one block that must NOT quote pawl's live value.

    `source_paths = ["mech"]` is correct here and wrong everywhere else: this is
    the only repo where `mech` is the source (REQ-ARCH-4). A deployment that
    copies it mutates vendored code whose survivors are the rows in *this*
    repo's `MUTANTS.md` — a file that does not travel — so `ci_weekly` scores all
    set as new unclassified survivors and goes red on the first maintenance run.
    The inventory shipped exactly that value until 2026-07-29, and told adopters
    to *add* their packages alongside it.
    """
    assert "mech" not in _inventory_mutmut_table()["source_paths"]


def test_stock_and_deployment_mutmut_configs_rerun_after_dependency_changes():
    live = tomllib.loads(_read("pyproject.toml"))["tool"]["mutmut"]
    shipped = _inventory_mutmut_table()
    assert live["on_dependency_change"] == "rerun"
    assert shipped["on_dependency_change"] == "rerun"
    assert '"mutmut>=3.7.0"' in _read("pyproject.toml")
    assert '"mutmut>=3.7.0"' in _read_skill(
        "pawl-adopt", "references", "file-inventory.md"
    )


def test_stock_and_deployment_configs_share_the_scoped_strict_list():
    live = json.loads(_read("pyrightconfig.json"))
    shipped = _inventory_pyright_config()
    assert live["typeCheckingMode"] == "standard"
    assert live["strict"] == STRICT_PAWL_MODULES
    assert shipped["strict"] == STRICT_PAWL_MODULES

    instructions = _read_skill("pawl-adopt", "references", "project-instructions.md")
    assert "Typing ratchet (scoped)" in instructions
    assert "strict list in pyrightconfig.json is empty" not in instructions
    for path in STRICT_PAWL_MODULES:
        assert path in instructions


def test_pawl_owns_its_suite_under_mech():
    root = _root()
    assert os.path.isfile(os.path.join(root, "mech", "tests", "__init__.py"))
    assert not os.path.isdir(os.path.join(root, "tests"))

    live = tomllib.loads(_read("pyproject.toml"))["tool"]
    assert live["pytest"]["ini_options"]["testpaths"] == ["mech/tests"]
    assert live["mutmut"]["pytest_add_cli_args_test_selection"] == ["mech/tests/"]
    assert live["mutmut"]["do_not_mutate"] == ["mech/tests/*"]
    assert live["coverage"]["run"]["source"] == ["mech"]
    assert "mech/tests/*" in live["coverage"]["run"]["omit"]


def test_stock_and_deployment_mutation_do_not_seed_a_disabled_plugin():
    live = tomllib.loads(_read("pyproject.toml"))["tool"]["mutmut"]
    shipped = _inventory_mutmut_table()

    assert "pytest_add_cli_args" not in live
    assert "pytest_add_cli_args" not in shipped


def test_maintenance_workflow_runs_monthly_and_on_demand():
    workflow = _read(".github", "workflows", "weekly.yml")

    assert "workflow_dispatch: {}" in workflow
    assert "schedule:" in workflow
    assert "cron: '0 4 1 * *'" in workflow


def test_deployment_inventory_keeps_product_tests_and_records_pawl_verification():
    doc = _read_skill("pawl-adopt", "references", "file-inventory.md")
    assert "tests/                         pawl's own suite" not in doc
    assert "mech/tests/" in doc
    assert 'testpaths = ["tests"]' in doc
    assert 'pytest_add_cli_args_test_selection = ["tests/"]' in doc
    assert 'pawl_selftest_record = ".pawl-selftest.json"' in doc


def test_adopt_and_sync_run_the_explicit_pawl_selftest_before_success():
    command = "uv run python -m mech.selftest --record"
    assert command in _read_skill("pawl-adopt", "SKILL.md")
    assert command in _read_skill("pawl-sync", "SKILL.md")


def test_inventory_quotes_the_live_ruff_config():
    """What the inventory ships is what pawl runs.

    This block has rotted before: it lost `PGH003`/`PGH004` while pyproject
    carried them, so a new deployment would have been born without the rule its
    own `AGENTS.md` states. `extend-exclude` is the same shape of hazard in the
    other direction — a deployment born without it lets `ruff format` rewrite
    Python inside Markdown once ruff reaches 0.16.
    """
    live = tomllib.loads(_read("pyproject.toml"))["tool"]["ruff"]
    shipped = _inventory_ruff_table()
    assert shipped["target-version"] == live["target-version"]
    assert shipped["extend-exclude"] == live["extend-exclude"]
    assert shipped["lint"]["select"] == live["lint"]["select"]
    assert shipped["lint"]["per-file-ignores"] == {
        **live["lint"]["per-file-ignores"],
        "tests/**": ["S101"],
    }


def test_arming_doc_quotes_the_live_hooks_json():
    """The documented block is what a new deployment gets. It drifted once
    already: the Stop command lost its cwd-placing prefix, and
    EXPECTED_SKELETON compares the module rather than the command string, so
    validation could not catch it."""
    live = json.loads(_read(".codex", "hooks.json"))
    assert live in _arming_json_blocks(), (
        "no json block in arming.md matches .codex/hooks.json"
    )


def test_documented_hooks_block_passes_validation():
    """Stronger than equality: what the doc ships must actually be accepted."""
    live = json.loads(_read(".codex", "hooks.json"))
    errors = validation.validate(
        json.dumps(live),
        _read(".githooks", "pre-commit"),
        ".githooks",
        in_ci=False,
        stop_hook_required=validation.stop_hook_required(_read("pyproject.toml")),
    )
    assert errors == []


def test_active_repository_surface_is_codex_only():
    """Runtime, instructions and deploy skills must not drift back to the
    pre-Codex harness. Dated docs, evals and telemetry remain historical
    records and are deliberately outside this check."""
    root = _root()
    assert os.path.isfile(os.path.join(root, "AGENTS.md"))
    assert os.path.isfile(os.path.join(root, ".codex", "hooks.json"))

    legacy_instruction = "CLAU" + "DE.md"
    legacy_dir = ".clau" + "de"
    assert not os.path.exists(os.path.join(root, legacy_instruction))
    assert not os.path.exists(os.path.join(root, legacy_dir))

    needles = (("clau" + "de").casefold(), "anth" + "ropic", legacy_dir)
    active_paths = [
        "AGENTS.md",
        "README.md",
        "BACKLOG.md",
        "MUTANTS.md",
        "pyproject.toml",
    ]
    for directory in ("mech", os.path.join(*PLUGIN_SKILLS_ROOT)):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(root, directory)):
            active_paths.extend(
                os.path.relpath(os.path.join(dirpath, name), root)
                for name in filenames
                if name.endswith((".md", ".py", ".json", ".toml"))
            )

    findings = []
    for relpath in active_paths:
        text = _read(relpath).casefold()
        if any(needle in text for needle in needles):
            findings.append(relpath)
    assert not findings, f"legacy harness coupling remains in: {sorted(findings)}"


@pytest.mark.parametrize("hook", validation.GATE_HOOKS)
def test_every_gate_hook_appears_in_the_copy_inventory(hook):
    """A hook pawl ships but the inventory does not list is a hook downstream
    never copies — which is how a deployment ends up with merge commits
    ungated while reporting itself armed."""
    assert hook in _read_skill("pawl-adopt", "references", "file-inventory.md")


@pytest.mark.parametrize("hook", validation.GATE_HOOKS)
def test_sync_diffs_every_gate_hook(hook):
    """pawl-sync is how an EXISTING deployment receives a new hook. If its diff
    step does not name the file, nobody is told it is missing."""
    assert hook in _read_skill("pawl-sync", "SKILL.md")


def test_sync_audits_every_nonverbatim_deployment_surface():
    """The sync cannot copy these whole, so omission makes drift invisible.

    This pins the diagnostic inventory rather than particular prose: Codex
    registration and interpreter pin are separate files, uv/dev settings are
    parsed merge surfaces, the target lock is compared package-by-package, and
    ignore rules are proved behaviorally.
    """
    doc = _read_skill("pawl-sync", "SKILL.md")
    for required in (
        ".codex/hooks.json",
        ".python-version",
        '("tool", "uv")',
        '("dependency-groups", "dev")',
        '"$PAWL/uv.lock"',
        "git check-ignore",
        '"$REPO/AGENTS.md"',
    ):
        assert required in doc, f"pawl-sync no longer audits {required}"


@pytest.mark.parametrize("hook", validation.GATE_HOOKS)
def test_mixed_repo_guide_ports_every_gate_hook(hook):
    doc = _read_skill("pawl-adopt", "references", "mixed-repo.md")
    assert hook in doc
    assert "copy `mech/` verbatim" in doc
    assert "--directory <project>" in doc
    assert "working-directory: <project>" in doc


def test_docs_do_not_contradict_the_workflows_uv_sync_flag():
    """`--frozen` installs from uv.lock without checking it matches
    pyproject.toml; `--locked` validates. The workflows switched on 2026-07-28
    and five doc references did not."""
    workflows = os.path.join(_root(), ".github", "workflows")
    flags = set()
    for name in sorted(os.listdir(workflows)):
        if name.endswith((".yml", ".yaml")):
            with open(os.path.join(workflows, name), encoding="utf-8") as fh:
                flags.update(re.findall(r"uv sync (--\S+)", fh.read()))
    assert flags, "no workflow runs `uv sync` — has the CI entry point moved?"

    for relpath, text in _skill_docs():
        for quoted in set(re.findall(r"uv sync (--\S+?)[`\s,.]", text)):
            assert quoted in flags, (
                f"{relpath} documents `uv sync {quoted}` but the workflows run "
                f"{sorted(flags)}"
            )


def test_push_workflow_fetches_the_history_its_diff_scope_needs():
    workflow = _read(".github", "workflows", "push.yml")
    assert "fetch-depth: 0" in workflow
    assert 'git fetch origin "$DEFAULT_BRANCH"' in workflow
    assert "uv run python -m mech.ci_push" in workflow


def test_workflows_pin_setup_uv_to_an_exact_release():
    """setup-uv does not publish a floating tag for every major.

    `@v10` passed every local gate but GitHub could not resolve it; the actual
    releases are `v10.0.0`, `v10.0.1`, and so on.  Require an exact release in
    every workflow so that unsupported floating-major assumptions stay out of
    the deployment payload.
    """
    refs = []
    workflows = os.path.join(_root(), ".github", "workflows")
    for name in sorted(os.listdir(workflows)):
        if name.endswith((".yml", ".yaml")):
            refs.extend(
                re.findall(
                    r"astral-sh/setup-uv@(\S+)",
                    _read(".github", "workflows", name),
                )
            )

    assert refs, "no workflow installs uv through astral-sh/setup-uv"
    assert all(re.fullmatch(r"v\d+\.\d+\.\d+", ref) for ref in refs), refs
    assert len(set(refs)) == 1, f"workflows disagree on setup-uv: {refs}"


def test_no_committed_doc_cites_an_untracked_path():
    """A citation nobody can open is worse than no citation.

    An unsourced claim at least reads as unsourced. A path reads as
    checkable and so invites nobody to check it — which is how two *stamped*
    documents came to source the survivor figures to
    `.superpowers/sdd/task-12-report.md` while `.gitignore` excluded the whole
    directory, leaving the claim resolvable only in the working tree that
    wrote it. That file is force-added for exactly this reason; this test is
    the rule it satisfies.
    """
    root = _root()
    tracked = set(
        subprocess.run(
            ["git", "ls-files"],  # noqa: S607  # PATH-resolved `git`; reads the index only
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    cited: set[str] = set()
    for rel in sorted(tracked):
        if rel.endswith(".md") and rel.startswith(("docs/", "plugins/pawl/skills/")):
            cited.update(re.findall(r"\.superpowers/[\w./-]+\.md", _read(rel)))
    missing = sorted(path for path in cited if path not in tracked)
    assert not missing, (
        f"committed docs cite paths git does not track: {missing} — "
        "commit them or stop citing them"
    )


def test_the_appendix_names_hand_running_and_every_quirk_it_covers():
    """B15/B41. One row per quirk, so a doc that dropped a row would teach an
    operator that a hazard is handled when it is not."""
    doc = _read_skill("pawl-adopt", "references", "brownfield.md")
    assert "quirks that bite when you run mutmut by hand" in doc
    for quirk in (
        "last-scoped-run state reset",
        "transient/crash state",
        "`--all true` needed for counts",
        "`no tests` is not a survivor",
        "`ǁ` quoting in class-method IDs",
    ):
        assert quirk in doc, f"the coverage table lost the row {quirk!r}"


def test_the_appendix_cites_the_sourced_survivor_figures():
    """B16. The replacement must be present, not merely the old one absent —
    deleting the sentence would otherwise pass."""
    doc = _read_skill("pawl-adopt", "references", "brownfield.md")
    assert "61 of 62" not in doc, "the unsourced survivor statistic is back"
    assert "task-12-report.md" in doc
    assert "122 survivors" in doc and "35" in doc


def test_the_recipe_pins_xdist_serialisation_and_the_timeout_formula():
    """B18/B39/B40/B59/B60: adapter-owned isolation and timeout policy."""
    doc = _read_skill("pawl-adopt", "references", "brownfield.md")
    assert "mutation.survey" in doc
    assert "authoritative scrub policy" in doc
    assert "PYTEST_ADDOPTS=-p xdist.plugin -n0" in doc
    assert "xdist.plugin" in doc
    assert "pytest_add_cli_args*" in doc and "Pawl rejects" in doc
    assert "Passing `env=` explicitly" in doc
    assert "ceil(T_clean" in doc, "the timeout formula lost its shape"
    assert "regenerate_tree" in doc, "mutant_count's non-circular source lost"
    assert "max(1" in doc, "the coefficient floor lost"


def test_the_inventory_states_a_dated_mutants_tree_size():
    """B17."""
    doc = _read_skill("pawl-adopt", "references", "file-inventory.md")
    assert "2–3 MB per module" not in doc, "the stale mutants/ size is back"
    assert re.search(r"measured 2026-\d\d-\d\d", doc), "no dated measurement"


def test_hypothesis_dir_is_ignored_here_and_in_the_shipped_block():
    """Hypothesis writes an example database at the repo root on any run that
    touches a property test. Untracked, it defeats a clean-tree precondition
    and trips the gate's index-mismatch warning for something the operator
    never created. Both `.hypothesis/` and `mutants/.hypothesis/` exist in
    this repo today."""
    assert ".hypothesis/" in _read(".gitignore")
    inventory = _read_skill("pawl-adopt", "references", "file-inventory.md")
    block = re.search(r"## `\.gitignore` additions\n+```\n(.*?)```", inventory, re.S)
    assert block, "file-inventory.md no longer has a .gitignore additions block"
    assert ".hypothesis/" in block.group(1)
