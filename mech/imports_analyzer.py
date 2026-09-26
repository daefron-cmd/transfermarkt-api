"""Static importer closure (spec REQ-CG-3a). Conservative over-inclusion.

Edges are discovered by `ast.parse` of every `.py` file under `root`,
walking `Import` and `ImportFrom` nodes only (string literals, comments,
and dynamic imports such as `importlib`/`__import__` never create edges —
the latter is a named residual). `import a.b` edges both `a` and `a.b`
(all dotted prefixes); `from a import b` edges `a` unconditionally, plus
`a.b` only when `a.b` is itself a real module discovered in this repo
(conservative symbol-vs-submodule handling — `b` is otherwise just a
name imported from `a`, not a submodule). Relative imports resolve
against the importing file's package. Star, conditional, `try/except
ImportError`, and `if TYPE_CHECKING` imports all count, because
`ast.walk` finds them regardless of the control-flow node they are
nested under.
"""

import ast
import os

SKIP_DIRS = {
    ".venv",
    ".git",
    "mutants",
    ".mech",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


class AnalyzerError(RuntimeError):
    pass


def _module_name(rel: str) -> str:
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(
    source: str,
    importer_module: str,
    filename: str,
    is_init: bool,
    all_modules: set[str],
) -> set[str]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise AnalyzerError(f"unparseable module {filename}: {exc}") from exc

    found: set[str] = set()
    name_parts = importer_module.split(".")
    # A package's own __init__.py IS the package (its __package__ equals its
    # __name__); a regular module's package is its name minus the last part.
    package_parts = name_parts if is_init else name_parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:  # REQ-CG-3a: `import a.b` edges a AND a.b
                parts = alias.name.split(".")
                found.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against importer's package
                base_parts = package_parts[: len(package_parts) - node.level + 1]
                prefix = ".".join(p for p in base_parts if p)
                if node.module:
                    prefix = f"{prefix}.{node.module}" if prefix else node.module
            else:
                prefix = node.module or ""
            if prefix:
                found.add(prefix)
            for alias in node.names:
                if alias.name == "*":  # star import: only the base module edge counts
                    continue
                candidate = f"{prefix}.{alias.name}" if prefix else alias.name
                if (
                    candidate in all_modules
                ):  # conservative symbol-vs-submodule handling
                    found.add(candidate)
    return {f for f in found if f}


def _edges(root: str) -> dict[str, set[str]]:
    files: list[tuple[str, str, str, bool]] = []  # (module, rel, source, is_init)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            module = _module_name(rel)
            with open(full, encoding="utf-8") as fh:
                source = fh.read()
            files.append((module, rel, source, name == "__init__.py"))

    all_modules = {module for module, _, _, _ in files}
    edges: dict[str, set[str]] = {}
    for module, rel, source, is_init in files:
        edges[module] = _imported_modules(source, module, rel, is_init, all_modules)
    return edges


def importers_closure(root: str, seed_modules: set[str]) -> set[str]:
    """Modules that transitively import any of `seed_modules` (REQ-CG-3a).

    Direction: returns *importers*, not imports. Cycles are handled by
    transitive closure over a worklist frontier. "No importers" yields the
    empty set (never the seed set itself). Callers own the seed-union —
    this function returns importers only; union in `seed_modules` yourself
    if you need the full affected set.
    """
    edges = _edges(root)
    closure: set[str] = set()
    frontier = set(seed_modules)
    while frontier:
        newly = {
            m
            for m, imports in edges.items()
            if m not in closure
            and m not in seed_modules
            and any(i == t or i.startswith(t + ".") for i in imports for t in frontier)
        }
        closure |= newly
        frontier = newly
    return closure
