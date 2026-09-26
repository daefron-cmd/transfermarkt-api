from pathlib import Path

import pytest

from mech import source_inventory


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("pkg" + chr(92) + "nested" + chr(92), "pkg/nested"),
        ("./pkg/./nested/", "pkg/nested"),
        (".", "."),
        ("/leading", "/leading"),
        ("pkg/ ", "pkg/ "),
        ("pkgX", "pkgX"),
    ],
)
def test_normalise_canonicalises_posix_relative_paths(raw, normalised):
    assert source_inventory._normalise(raw) == normalised


@pytest.mark.parametrize(
    "path", ["", "/absolute", "../escape", "pkg/../escape", "pkg\\..\\escape"]
)
def test_valid_relative_rejects_empty_absolute_and_parent_paths(path):
    assert source_inventory._valid_relative(path) is False


@pytest.mark.parametrize(
    "path", ["pkg", "pkg/nested.py", "pkg" + chr(92) + "nested.py"]
)
def test_valid_relative_accepts_nonempty_descendants(path):
    assert source_inventory._valid_relative(path) is True


def test_inventory_reads_mutmut_roots_and_exclusions():
    inventory = source_inventory.from_pyproject(
        """
[tool.mutmut]
source_paths = ["mech", "main.py"]
do_not_mutate = ["mech/tests/*"]
"""
    )

    assert inventory.source_paths == ("mech", "main.py")
    assert inventory.do_not_mutate == ("mech/tests/*",)
    assert inventory.module_for("mech/gate.py") == "mech.gate"
    assert inventory.module_for("mech/__init__.py") == "mech"
    assert inventory.module_for("main.py") == "main"
    assert inventory.module_for("mech/tests/test_gate.py") is None
    assert inventory.module_for("mech/tests/nested/test_more.py") is None


def test_repository_root_source_path_is_authoritative(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    inventory = source_inventory.from_pyproject('[tool.mutmut]\nsource_paths = ["."]\n')

    assert inventory.eligible("app.py") is True
    assert inventory.module_for("app.py") == "app"
    assert inventory.discover_python_files(str(tmp_path)) == [
        "app.py",
        "pkg/feature.py",
    ]


def test_only_mutate_narrows_the_same_inventory_used_by_push_and_coverage():
    inventory = source_inventory.from_pyproject(
        """
[tool.mutmut]
source_paths = ["./pkg"]
only_mutate = ["pkg/public_*.py"]
do_not_mutate = ["pkg/public_legacy.py"]
"""
    )

    assert inventory.source_paths == ("pkg",)
    assert inventory.only_mutate == ("pkg/public_*.py",)
    assert inventory.eligible("pkg/public_api.py") is True
    assert inventory.eligible("pkg/internal.py") is False
    assert inventory.eligible("pkg/public_legacy.py") is False


@pytest.mark.parametrize("path", ["pkg/../pkg/live.py", "pkg\\..\\pkg\\live.py"])
def test_eligibility_rejects_raw_parent_traversal(path):
    inventory = source_inventory.SourceInventory(
        source_paths=("pkg",), do_not_mutate=()
    )

    assert inventory.eligible(path) is False
    assert inventory.module_for(path) is None


def test_module_for_accepts_unicode_identifiers_and_rejects_invalid_components():
    inventory = source_inventory.SourceInventory(
        source_paths=("pkg",), do_not_mutate=()
    )

    assert inventory.module_for("pkg/café.py") == "pkg.café"
    assert inventory.module_for("pkg/space name.py") is None
    assert inventory.module_for("pkg/tab\tname.py") is None


def test_inventory_requires_explicit_nonempty_source_paths():
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.from_pyproject("[tool.mutmut]\nsource_paths = []\n")
    assert str(error.value) == (
        "[tool.mutmut] source_paths must be nonempty relative paths"
    )


def test_inventory_names_unparseable_toml_exactly():
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.from_pyproject("[")
    assert str(error.value).startswith("pyproject.toml is unparseable:")


@pytest.mark.parametrize("invalid", ["[123]", '["../escape"]', '[""]'])
def test_inventory_rejects_nonstring_escaping_and_empty_source_entries(invalid):
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.from_pyproject(f"[tool.mutmut]\nsource_paths = {invalid}\n")
    assert str(error.value) == (
        "[tool.mutmut] source_paths must be nonempty relative paths"
    )


@pytest.mark.parametrize("invalid", ['"not-a-list"', "[1]", '[""]'])
def test_inventory_rejects_invalid_exclusion_shapes(invalid):
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.from_pyproject(
            f'[tool.mutmut]\nsource_paths = ["pkg"]\ndo_not_mutate = {invalid}\n'
        )
    assert str(error.value) == ("[tool.mutmut] do_not_mutate must contain glob strings")


@pytest.mark.parametrize("invalid", ['"not-a-list"', "[1]", '[""]'])
def test_inventory_rejects_invalid_only_mutate_shapes(invalid):
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.from_pyproject(
            f'[tool.mutmut]\nsource_paths = ["pkg"]\nonly_mutate = {invalid}\n'
        )
    assert str(error.value) == "[tool.mutmut] only_mutate must contain glob strings"


def test_discover_python_files_uses_the_same_eligibility_rule(tmp_path: Path):
    (tmp_path / "pkg" / "tests" / "nested").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "live.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "tests" / "test_live.py").write_text(
        "def test_x(): pass\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "tests" / "nested" / "helper.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")

    inventory = source_inventory.SourceInventory(
        source_paths=("pkg",), do_not_mutate=("pkg/tests/*",)
    )

    assert inventory.discover_python_files(str(tmp_path)) == [
        "pkg/__init__.py",
        "pkg/live.py",
    ]


def test_discovery_accepts_a_configured_python_file_and_skips_a_non_python_file(
    tmp_path: Path,
):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not source\n", encoding="utf-8")
    inventory = source_inventory.SourceInventory(
        source_paths=("main.py", "notes.txt"), do_not_mutate=()
    )

    assert inventory.discover_python_files(str(tmp_path)) == ["main.py"]


def test_discovery_fails_closed_when_a_configured_source_is_missing(tmp_path: Path):
    inventory = source_inventory.SourceInventory(
        source_paths=("missing",), do_not_mutate=()
    )

    with pytest.raises(source_inventory.InventoryError, match="missing"):
        inventory.discover_python_files(str(tmp_path))


def test_load_names_the_missing_pyproject_path(tmp_path: Path):
    with pytest.raises(source_inventory.InventoryError) as error:
        source_inventory.load(str(tmp_path))

    message = str(error.value)
    assert message.startswith("pyproject.toml is unreadable:")
    assert "pyproject.toml" in message
