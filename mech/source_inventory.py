"""Canonical mutation and coverage source inventory.

`[tool.mutmut]` already names the product source in Pawl and in every
deployment.  Reading the same roots and exclusions here keeps coverage
reachability from inventing a second, Pawl-specific definition of source.
"""

import fnmatch
import os
import posixpath
import tomllib
from dataclasses import dataclass
from typing import cast


class InventoryError(ValueError):
    """The configured source inventory cannot produce an authoritative list."""


def _normalise(path: str) -> str:
    path = path.replace("\\", "/")
    return posixpath.normpath(path) if path else ""


def _valid_relative(path: str) -> bool:
    separated = path.replace("\\", "/")
    normalised = _normalise(path)
    return (
        bool(normalised)
        and not os.path.isabs(normalised)
        and ".." not in separated.split("/")
    )


@dataclass(frozen=True)
class SourceInventory:
    source_paths: tuple[str, ...]
    do_not_mutate: tuple[str, ...]
    only_mutate: tuple[str, ...] = ()

    def eligible(self, rel_path: str) -> bool:
        """Whether mutmut may generate mutants for this repository-relative path."""
        if not _valid_relative(rel_path):
            return False
        rel = _normalise(rel_path)
        if not rel.endswith(".py"):
            return False
        under_source = any(
            source == "." or rel == source or rel.startswith(f"{source}/")
            for source in map(_normalise, self.source_paths)
        )
        if not under_source:
            return False
        if self.only_mutate and not any(
            fnmatch.fnmatch(rel, pattern) for pattern in self.only_mutate
        ):
            return False
        return not any(fnmatch.fnmatch(rel, pattern) for pattern in self.do_not_mutate)

    def module_for(self, rel_path: str) -> "str | None":
        if not self.eligible(rel_path):
            return None
        dotted = _normalise(rel_path)[:-3].replace("/", ".")
        module = dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted
        return (
            module if all(part.isidentifier() for part in module.split(".")) else None
        )

    def discover_python_files(self, root: str) -> list[str]:
        found: set[str] = set()
        for configured in self.source_paths:
            source = _normalise(configured)
            full = os.path.join(root, *source.split("/"))
            if os.path.isfile(full):
                if self.eligible(source):
                    found.add(source)
                continue
            if not os.path.isdir(full):
                raise InventoryError(f"configured source path is missing: {source}")
            for dirpath, _dirnames, filenames in os.walk(full):
                for filename in filenames:
                    if not filename.endswith(".py"):
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, filename), root)
                    rel = rel.replace(os.sep, "/")
                    if self.eligible(rel):
                        found.add(rel)
        return sorted(found)


def from_pyproject(text: str) -> SourceInventory:
    try:
        data = cast(dict[str, object], tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise InventoryError(f"pyproject.toml is unparseable: {exc}") from exc
    tool_value = data.get("tool")
    tool = cast(dict[str, object], tool_value) if isinstance(tool_value, dict) else None
    mutmut_value = tool.get("mutmut") if tool is not None else None
    mutmut = (
        cast(dict[str, object], mutmut_value)
        if isinstance(mutmut_value, dict)
        else None
    )
    raw_sources: object = mutmut.get("source_paths") if mutmut is not None else None
    raw_exclusions: object = (
        mutmut.get("do_not_mutate", []) if mutmut is not None else []
    )
    raw_inclusions: object = mutmut.get("only_mutate", []) if mutmut is not None else []
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or not all(
            isinstance(path, str) and _valid_relative(path)
            for path in cast(list[object], raw_sources)
        )
    ):
        raise InventoryError(
            "[tool.mutmut] source_paths must be nonempty relative paths"
        )
    if not isinstance(raw_exclusions, list) or not all(
        isinstance(pattern, str) and pattern
        for pattern in cast(list[object], raw_exclusions)
    ):
        raise InventoryError("[tool.mutmut] do_not_mutate must contain glob strings")
    if not isinstance(raw_inclusions, list) or not all(
        isinstance(pattern, str) and pattern
        for pattern in cast(list[object], raw_inclusions)
    ):
        raise InventoryError("[tool.mutmut] only_mutate must contain glob strings")
    sources = cast(list[str], raw_sources)
    exclusions = cast(list[str], raw_exclusions)
    inclusions = cast(list[str], raw_inclusions)
    return SourceInventory(
        source_paths=tuple(_normalise(path) for path in sources),
        do_not_mutate=tuple(_normalise(pattern) for pattern in exclusions),
        only_mutate=tuple(_normalise(pattern) for pattern in inclusions),
    )


def load(root: str) -> SourceInventory:
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            data = fh.read().decode("utf-8")
    except OSError as exc:
        raise InventoryError(f"pyproject.toml is unreadable: {exc}") from exc
    return from_pyproject(data)
