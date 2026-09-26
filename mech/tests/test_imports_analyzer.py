import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mech import imports_analyzer


def _mkrepo(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def test_direct_and_transitive_importers(tmp_path):
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "x = 1\n",
            "pkg/mid.py": "from pkg import core\n",
            "pkg/top.py": "import pkg.mid\n",
            "tests/test_top.py": "import pkg.top\n",
            "unrelated.py": "y = 2\n",
        },
    )
    closure = imports_analyzer.importers_closure(root, {"pkg.core"})
    assert closure == {"pkg.mid", "pkg.top", "tests.test_top"}


def test_relative_import_resolved(tmp_path):
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "",
            "pkg/sib.py": "from . import core\n",
        },
    )
    assert "pkg.sib" in imports_analyzer.importers_closure(root, {"pkg.core"})


def test_conditional_import_counts(tmp_path):
    root = _mkrepo(
        tmp_path,
        {
            "a.py": "",
            "b.py": "try:\n    import a\nexcept ImportError:\n    pass\n",
        },
    )
    assert "b" in imports_analyzer.importers_closure(root, {"a"})


def test_unparseable_file_fails_closed(tmp_path):
    root = _mkrepo(tmp_path, {"broken.py": "def f(:\n", "a.py": ""})
    with pytest.raises(imports_analyzer.AnalyzerError):
        imports_analyzer.importers_closure(root, {"a"})


def test_analyzer_error_names_the_real_file(tmp_path):
    # Fail-closed diagnostics must name the offending file precisely: the
    # message carries our path AND ast's own detail must not degrade to the
    # "<unknown>" placeholder (which happens if the filename is not passed
    # through to ast.parse).
    root = _mkrepo(tmp_path, {"broken.py": "def f(:\n", "a.py": ""})
    with pytest.raises(imports_analyzer.AnalyzerError) as excinfo:
        imports_analyzer.importers_closure(root, {"a"})
    msg = str(excinfo.value)
    assert "unparseable module broken.py" in msg
    assert "<unknown>" not in msg


def test_star_import_counts(tmp_path):  # REQ-CG-3a conservative over-inclusion
    root = _mkrepo(
        tmp_path,
        {
            "a.py": "",
            "b.py": "from a import *\n",
        },
    )
    assert "b" in imports_analyzer.importers_closure(root, {"a"})


def test_type_checking_import_counts(tmp_path):  # REQ-CG-3a conservative over-inclusion
    root = _mkrepo(
        tmp_path,
        {
            "a.py": "",
            "b.py": (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import a\n"
            ),
        },
    )
    assert "b" in imports_analyzer.importers_closure(root, {"a"})


def test_import_dotted_edges(tmp_path):  # `import a.b` edges BOTH a and a.b
    root = _mkrepo(
        tmp_path,
        {
            "a/__init__.py": "",
            "a/b.py": "",
            "c.py": "import a.b\n",
        },
    )
    assert "c" in imports_analyzer.importers_closure(root, {"a"})
    assert "c" in imports_analyzer.importers_closure(root, {"a.b"})


def test_nested_module_relative_import_resolves_against_own_package(tmp_path):
    # A REGULAR module two packages deep: pkg/sub/mod.py's package is
    # "pkg.sub" (name minus last part). If the package computation regressed
    # to keeping only the first part, `from . import x` would resolve against
    # "pkg" and the real pkg.sub.x edge would vanish.
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/x.py": "",
            "pkg/sub/mod.py": "from . import x\n",
        },
    )
    assert "pkg.sub.mod" in imports_analyzer.importers_closure(root, {"pkg.sub.x"})


def test_two_level_relative_import_climbs_correct_distance(tmp_path):
    # `from .. import y` in pkg/sub/mod.py must resolve against "pkg", not
    # "pkg.sub": each relative level beyond the first climbs one package.
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/y.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": "from .. import y\n",
        },
    )
    assert "pkg.sub.mod" in imports_analyzer.importers_closure(root, {"pkg.y"})


def test_relative_import_with_module_name(tmp_path):
    # `from .m import x` edges pkg.m — the dotted-module form of a relative
    # import, distinct from the bare `from . import m` form already covered.
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "x = 1\n",
            "pkg/sib.py": "from .m import x\n",
        },
    )
    assert "pkg.sib" in imports_analyzer.importers_closure(root, {"pkg.m"})


def test_absolute_dotted_from_import_matches_prefix_seed(tmp_path):
    # `from a.b import c` records only the "a.b" edge (ImportFrom does no
    # prefix expansion) — closure membership for seed "a" therefore depends
    # on the prefix (startswith) matching rule in REQ-CG-3a.
    root = _mkrepo(
        tmp_path,
        {
            "a/__init__.py": "",
            "a/b.py": "c = 1\n",
            "consumer.py": "from a.b import c\n",
        },
    )
    assert "consumer" in imports_analyzer.importers_closure(root, {"a"})


def test_init_relative_import_package_parts(tmp_path):
    # `pkg/sub/__init__.py`'s own module IS the package "pkg.sub" (is_init),
    # not "pkg" (name_parts[:-1]) — a nested package's `__init__.py` must
    # resolve `from . import x` against its own dotted name, not its
    # parent's. If the is_init handling regressed, `from . import x` here
    # would resolve against "pkg" instead, mapping to nonexistent "pkg.x"
    # and missing the real "pkg.sub.x" edge entirely.
    root = _mkrepo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "from . import x\n",
            "pkg/sub/x.py": "",
        },
    )
    assert "pkg.sub" in imports_analyzer.importers_closure(root, {"pkg.sub.x"})


# --- REQ-CG-3a closure property ----------------------------------------------
#
# The examples above pin the import *forms* (relative, star, dotted, TYPE_CHECKING).
# What none of them can pin is the *graph* the worklist walks, so this covers the
# other axis: arbitrary topology — cycles, self-imports, diamonds, disconnected
# components, several seeds at once — over the one import form whose edge set is
# unambiguous (`import <module>`, flat modules at the repo root).
GRAPH_MODULES = ("m0", "m1", "m2", "m3", "m4")
_import_graphs = st.fixed_dictionaries(
    {module: st.sets(st.sampled_from(GRAPH_MODULES)) for module in GRAPH_MODULES}
)


def _reaches(imports: dict) -> dict:
    """Transitive closure of the import relation, by repeated union to fixpoint.

    Deliberately a different algorithm from the frontier walk under test:
    forward reachability per module, iterated until nothing grows.
    """
    reach = {module: set(targets) for module, targets in imports.items()}
    changed = True
    while changed:
        changed = False
        for module in reach:
            grown = reach[module] | {t for i in reach[module] for t in reach[i]}
            if grown != reach[module]:
                reach[module] = grown
                changed = True
    return reach


@given(_import_graphs, st.sets(st.sampled_from(GRAPH_MODULES)))
def test_closure_is_exactly_the_modules_that_reach_a_seed(imports, seeds):
    """REQ-CG-3a: importers, transitively, and never the seeds themselves.

    A missing importer is the failure that matters — the commit gate would then
    type-check a file without the modules that depend on it.
    """
    with tempfile.TemporaryDirectory() as root:
        for module, targets in imports.items():
            Path(root, f"{module}.py").write_text(
                "".join(f"import {target}\n" for target in sorted(targets))
            )
        closure = imports_analyzer.importers_closure(root, set(seeds))

    reach = _reaches(imports)
    assert closure == {m for m in imports if m not in seeds and reach[m] & seeds}
