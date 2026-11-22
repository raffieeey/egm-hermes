"""Breadth-first repository discovery and work partitioning for EGM bootstrap.

``discover`` maps a directory tree level by level with an explicit queue, never
``os.walk`` or a recursive search, so deep trees and symlink loops cannot recurse
forever. ``prune`` removes test-only and code-free directories bottom-up,
``partition`` applies the upstream adaptive-depth partitioning rule, and
``build_plan`` bundles the result into the JSON-ready payload consumed by the
``egm_bootstrap_plan`` tool.

Discovery rules:

* Source files are matched by extension only (the upstream EGM list). Markdown
  and other documentation are never source, so a docs-only directory has zero
  source files and is pruned later.
* ``source_lines`` counts only a directory's *direct* source files, reading each
  file with ``errors="ignore"`` and stopping at 200,000 lines.
* Symlinks are never followed (neither to files nor to directories).
* Hidden directories and the usual build/system directories are skipped, as are
  matches from ``.gitignore``, ``.egmignore`` and the caller's ``ignore`` list.

Ignore-file glob subset (documented, deliberately smaller than git):

* blank lines and ``#`` comments are skipped;
* ``!`` negation lines are ignored, so nothing is ever re-included;
* a trailing ``/`` makes the pattern directory-only;
* a leading ``/`` anchors the pattern to the directory that owns the ignore file;
* ``*`` and ``?`` never cross ``/``, ``**`` does;
* a pattern containing ``/`` matches the path relative to its ignore file, a
  pattern without ``/`` matches the entry's basename at any depth;
* every ignore file applies to its own directory's subtree, like git.

When ``max_dirs`` is hit, discovery stops and ``Tree.truncated`` is recorded so
callers can warn instead of silently planning a partial tree. A plan counts the
repository root as a directory, so ``directory_count`` is one more than the
number of subdirectories and the safety gate fires when more than 15 directories
survive pruning.
"""

from __future__ import annotations

import fnmatch
import os
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "DirNode",
    "MAX_SOURCE_LINES_PER_FILE",
    "PartitionPlan",
    "SOURCE_EXTENSIONS",
    "TEST_DIR_NAMES",
    "TEST_FILE_PATTERNS",
    "Tree",
    "build_plan",
    "discover",
    "partition",
    "prune",
    "verification_command",
]

#: Source, schema, contract and infrastructure-code extensions from the EGM spec.
SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    # Systems & compiled
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".rs", ".go", ".zig",
    ".lean", ".f", ".f90", ".f95", ".f03", ".f08", ".hs",
    # Managed & enterprise
    ".java", ".kt", ".scala", ".cs", ".razor", ".cshtml", ".fs", ".vb",
    # Scripting & dynamic
    ".py", ".rb", ".php", ".lua", ".sh", ".bash", ".ps1", ".psm1", ".psd1",
    ".r", ".R", ".ex", ".exs",
    # Web & mobile
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".swift", ".m", ".dart",
    # Data & schemas / IDLs
    ".sql", ".proto", ".graphql", ".gql", ".thrift", ".prisma",
    # Infrastructure / declarative
    ".tf", ".hcl", ".cue",
})

#: Directories that are never walked, regardless of ignore files.
DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "venv", ".venv", "dist", "build", "third_party",
    "__pycache__", ".tox", "target",
})

#: Directory names whose contents are treated as tests.
TEST_DIR_NAMES: frozenset[str] = frozenset({
    "tests", "testing", "testdata", "__tests__", "spec",
})

#: Filename globs recognised as test files.
TEST_FILE_PATTERNS: tuple[str, ...] = ("*_test.*", "*Test.*", "test_*.*", "*.spec.*")

#: Per-file line-count ceiling; larger files are counted as exactly this many lines.
MAX_SOURCE_LINES_PER_FILE = 200_000

_MANIFEST_NAMES: frozenset[str] = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "go.work",
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini",
    "requirements.txt", "Pipfile", "poetry.lock", "uv.lock",
    "Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "meson.build",
    "BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel",
    "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
    "mix.exs", "Package.swift",
})

_MANIFEST_SUFFIXES: tuple[str, ...] = (".sln", ".slnx", ".csproj", ".fsproj", ".vbproj")


@dataclass
class DirNode:
    """One directory in the discovery map; ``parent`` and ``children`` are root-relative paths."""

    path: str
    depth: int
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    source_lines: int = 0
    has_manifest: bool = False
    test_only: bool = False
    _file_lines: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready view of this directory node."""
        return {
            "path": self.path,
            "depth": self.depth,
            "parent": self.parent,
            "children": list(self.children),
            "source_files": list(self.source_files),
            "source_lines": self.source_lines,
            "has_manifest": self.has_manifest,
            "test_only": self.test_only,
        }


@dataclass
class Tree:
    """A discovered directory tree: nodes keyed by root-relative POSIX path."""

    root: Path
    nodes: dict[str, DirNode]
    ignored: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def code_dirs(self) -> tuple[str, ...]:
        """Root-relative paths of directories holding direct source files."""
        return tuple(sorted(path for path, node in self.nodes.items() if node.source_files))

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready view of the whole tree."""
        nodes = sorted(self.nodes.values(), key=lambda node: node.path)
        return {
            "root": str(self.root),
            "truncated": self.truncated,
            "directory_count": len(nodes),
            "max_depth": max((node.depth for node in nodes), default=0),
            "ignored": list(self.ignored),
            "code_dirs": list(self.code_dirs),
            "nodes": [node.as_dict() for node in nodes],
        }


@dataclass(frozen=True)
class PartitionPlan:
    """The subtree roots one bootstrap run should fan out to."""

    roots: tuple[str, ...]
    depth: int
    reason: str
    worker_count: int

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready view of this partition plan."""
        return {
            "roots": list(self.roots),
            "depth": self.depth,
            "reason": self.reason,
            "worker_count": self.worker_count,
        }


@dataclass(frozen=True)
class _IgnoreRule:
    """One parsed ignore-file pattern, scoped to its owning directory."""

    base: str
    pattern: str
    dir_only: bool = False
    anchored: bool = False


def discover(root: str | Path, *, ignore: Sequence[str] = (), max_dirs: int = 5000) -> Tree:
    """Map ``root`` breadth-first into a :class:`Tree` of directory nodes."""
    root_path = Path(os.path.abspath(os.fspath(root)))
    if not root_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {root_path}")
    limit = max(1, int(max_dirs))
    patterns: Sequence[str] = (ignore,) if isinstance(ignore, str) else ignore
    custom_rules = tuple(
        rule for rule in (_parse_rule(text, "") for text in patterns) if rule is not None
    )
    root_node = DirNode(path=".", depth=0)
    nodes: dict[str, DirNode] = {".": root_node}
    ignored: set[str] = set()
    truncated = False
    queue: deque[tuple[Path, str, int, tuple[_IgnoreRule, ...]]] = deque(
        [(root_path, ".", 0, custom_rules)]
    )

    while queue and not truncated:
        dir_abs, rel, depth, inherited = queue.popleft()
        rules = inherited + _load_rules(dir_abs, "" if rel == "." else rel)
        try:
            with os.scandir(dir_abs) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            child_rel = name if rel == "." else f"{rel}/{name}"
            try:
                if entry.is_symlink():
                    ignored.add(child_rel)
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name.startswith(".") or name in DEFAULT_IGNORED_DIRS:
                    ignored.add(child_rel)
                    continue
                if _is_ignored(child_rel, True, rules):
                    ignored.add(child_rel)
                    continue
                if len(nodes) >= limit:
                    truncated = True
                    break
                nodes[child_rel] = DirNode(path=child_rel, depth=depth + 1, parent=rel)
                nodes[rel].children.append(child_rel)
                queue.append((Path(entry.path), child_rel, depth + 1, rules))
                continue
            if not is_file:
                continue
            if _is_ignored(child_rel, False, rules):
                ignored.add(child_rel)
                continue
            if _is_manifest(name):
                nodes[rel].has_manifest = True
            if _source_extension(name) is None:
                continue
            line_count = _count_lines(Path(entry.path))
            nodes[rel].source_files.append(name)
            nodes[rel]._file_lines[name] = line_count
            nodes[rel].source_lines += line_count

    return Tree(root=root_path, nodes=nodes, ignored=tuple(sorted(ignored)), truncated=truncated)


def prune(tree: Tree) -> Tree:
    """Return a copy of ``tree`` with test-only and code-free directories removed."""
    if "." not in tree.nodes:
        raise ValueError("tree has no root node")
    nodes = {path: _clone_node(node) for path, node in tree.nodes.items()}

    for node in nodes.values():
        kept_files = [name for name in node.source_files if not _is_test_file(name)]
        if len(kept_files) == len(node.source_files):
            continue
        node.source_files = kept_files
        if not kept_files:
            node.source_lines = 0
        elif node._file_lines:
            node.source_lines = sum(node._file_lines.get(name, 0) for name in kept_files)

    keep: dict[str, bool] = {}
    for node in sorted(nodes.values(), key=lambda item: (-item.depth, item.path)):
        if node.path == ".":
            keep[node.path] = True
        elif node.test_only or _is_test_dir_name(node.path):
            keep[node.path] = False
        else:
            keep[node.path] = bool(node.source_files) or any(
                keep.get(child, False) for child in node.children
            )

    pruned: dict[str, DirNode] = {}
    for path, node in nodes.items():
        if not keep.get(path):
            continue
        node.children = sorted(child for child in node.children if keep.get(child))
        pruned[path] = node

    return Tree(root=tree.root, nodes=pruned, ignored=tuple(tree.ignored),
                truncated=tree.truncated)


def partition(tree: Tree, *, max_workers: int = 5) -> PartitionPlan:
    """Apply the upstream adaptive-depth partitioning rule to a pruned tree."""
    nodes = tree.nodes
    if "." not in nodes:
        raise ValueError("tree has no root node")
    limit = max(1, int(max_workers))
    total = len(nodes) - 1
    if total <= 0:
        return PartitionPlan(
            (".",), 0,
            "the tree has no code directories; the repository root is the only partition.", 1)
    if limit == 1:
        return PartitionPlan(
            (".",), 0,
            "max_workers=1 forces the repository root as a single partition.", 1)
    if total < 3:
        noun = "directory exists" if total == 1 else "directories exist"
        return PartitionPlan(
            (".",), 0,
            f"only {total} code {noun}; the repository root is one partition.", 1)

    for depth in (1, 2, 3):
        level = sorted(path for path, node in nodes.items() if node.depth == depth)
        if not level:
            continue
        if 3 <= len(level) <= 5:
            chosen = tuple(level[:limit])
            reason = (f"depth {depth} holds {len(level)} code directories, "
                      "inside the 3-5 target range.")
            if len(chosen) < len(level):
                reason += f" Capped at max_workers={limit}."
            return PartitionPlan(chosen, depth, reason, len(chosen))
        if len(level) > 5:
            roots, fallback, kept = _group_level(nodes, level, limit)
            branch_word = "branch" if kept == 1 else "branches"
            reason = (f"depth {depth} holds {len(level)} code directories, over 5; kept the "
                      f"{kept} largest {branch_word} and grouped the remaining "
                      f"{len(level) - kept} under '{fallback}' as one fallback.")
            return PartitionPlan(roots, depth, reason, len(roots))

    immediate = tuple(sorted(
        path for path, node in nodes.items() if node.depth == 1
    )[:limit])
    reason = (f"no depth between 1 and 3 held 3-5 code directories; using the root's "
              f"{len(immediate)} immediate "
              f"{'child' if len(immediate) == 1 else 'children'}.")
    return PartitionPlan(immediate, 1, reason, len(immediate))


def verification_command(root: str | Path) -> str:
    """Pick the project's test command from its build manifests."""
    base = Path(os.path.abspath(os.fspath(root)))

    def has(name: str) -> bool:
        return (base / name).is_file()

    if has("BUILD") or has("BUILD.bazel"):
        return "bazel test //..."
    if has("pnpm-lock.yaml"):
        return "pnpm test"
    if has("yarn.lock"):
        return "yarn test"
    if has("package.json"):
        return "npm test"
    if has("pyproject.toml") or has("pytest.ini") or has("setup.py"):
        return "pytest"
    if has("Cargo.toml"):
        return "cargo test"
    if has("go.mod"):
        return "go test ./..."
    if any(base.glob("*.sln")) or any(base.glob("*.slnx")) or any(base.glob("*.csproj")):
        return "dotnet test"
    if has("Makefile"):
        return "make test"
    return "<test-command>"


def build_plan(root: str | Path, *, target_filename: str = "README.md",
               max_workers: int = 5, ignore: Sequence[str] = ()) -> dict[str, Any]:
    """Bundle discovery, pruning, partitioning and verification into one plan dict."""
    root_path = Path(os.path.abspath(os.fspath(root)))
    tree = prune(discover(root_path, ignore=ignore))
    plan = partition(tree, max_workers=max_workers)
    nodes = sorted(tree.nodes.values(), key=lambda node: node.path)
    return {
        "root": str(root_path),
        "target_filename": str(target_filename),
        "verification_command": verification_command(root_path),
        "directory_count": len(nodes),
        "max_depth": max((node.depth for node in nodes), default=0),
        "code_directories": list(tree.code_dirs),
        "partitions": plan.as_dict(),
        "needs_safety_gate": len(nodes) > 15,
        "nodes": [
            {
                "path": node.path,
                "depth": node.depth,
                "children": list(node.children),
                "source_files": list(node.source_files),
                "source_lines": node.source_lines,
                "has_manifest": node.has_manifest,
                "test_only": node.test_only,
                "summary": _node_summary(node),
            }
            for node in nodes
        ],
    }


def _node_summary(node: DirNode) -> str:
    """Describe one node in a short deterministic sentence."""
    files = len(node.source_files)
    children = len(node.children)
    code = (f"{files} source file{'s' if files != 1 else ''} ({node.source_lines} lines)"
            if files else "no direct source files")
    kids = (f"{children} child director{'ies' if children != 1 else 'y'}"
            if children else "no child directories")
    return f"{code}; {kids}"


def _source_extension(name: str) -> str | None:
    """Return the file's extension when it is a recognised source extension."""
    extension = os.path.splitext(name)[1]
    return extension if extension in SOURCE_EXTENSIONS else None


def _is_manifest(name: str) -> bool:
    """True when a filename is a package or build manifest."""
    return name in _MANIFEST_NAMES or name.endswith(_MANIFEST_SUFFIXES)


def _is_test_file(name: str) -> bool:
    """True when a filename matches one of the test-file globs."""
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in TEST_FILE_PATTERNS)


def _is_test_dir_name(path: str) -> bool:
    """True when the directory's basename marks it as test-only."""
    return path.rsplit("/", 1)[-1].lower() in TEST_DIR_NAMES


def _clone_node(node: DirNode) -> DirNode:
    """Copy a node so pruning never mutates the input tree."""
    return DirNode(
        path=node.path,
        depth=node.depth,
        parent=node.parent,
        children=list(node.children),
        source_files=list(node.source_files),
        source_lines=node.source_lines,
        has_manifest=node.has_manifest,
        test_only=node.test_only,
        _file_lines=dict(node._file_lines) if node._file_lines else {},
    )


def _count_lines(path: Path) -> int:
    """Count a file's lines, ignoring decode errors and capping the total."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            total = 0
            for _ in handle:
                total += 1
                if total >= MAX_SOURCE_LINES_PER_FILE:
                    break
            return total
    except OSError:
        return 0


def _parse_rule(text: str, base: str) -> _IgnoreRule | None:
    """Parse one ignore-file line into a rule, or ``None`` when it is skipped."""
    line = str(text or "").strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("!"):
        # Documented subset: negations are ignored, nothing is re-included.
        return None
    if line.startswith("\\#") or line.startswith("\\!"):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    anchored = line.startswith("/")
    line = line.lstrip("/")
    if not line:
        return None
    return _IgnoreRule(base=base, pattern=line, dir_only=dir_only, anchored=anchored)


def _load_rules(directory: Path, rel: str) -> tuple[_IgnoreRule, ...]:
    """Read ``.gitignore`` and ``.egmignore`` in one directory."""
    rules: list[_IgnoreRule] = []
    for filename in (".gitignore", ".egmignore"):
        try:
            text = (directory / filename).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            rule = _parse_rule(line, rel)
            if rule is not None:
                rules.append(rule)
    return tuple(rules)


def _is_ignored(rel_path: str, is_dir: bool, rules: Sequence[_IgnoreRule]) -> bool:
    """Apply ignore rules to one root-relative path."""
    name = rel_path.rsplit("/", 1)[-1]
    for rule in rules:
        target = rel_path
        if rule.base:
            prefix = rule.base + "/"
            if not rel_path.startswith(prefix):
                continue
            target = rel_path[len(prefix):]
        if rule.dir_only and not is_dir:
            continue
        if "/" in rule.pattern:
            if _glob_match(rule.pattern, target):
                return True
        elif rule.anchored:
            if _glob_match(rule.pattern, target):
                return True
        elif _glob_match(rule.pattern, name):
            return True
    return False


def _glob_match(pattern: str, text: str) -> bool:
    """Match one path against the documented glob subset."""
    @lru_cache(maxsize=None)
    def step(pattern_index: int, text_index: int) -> bool:
        if pattern_index == len(pattern):
            return text_index == len(text)
        char = pattern[pattern_index]
        if char == "*":
            if pattern[pattern_index + 1:pattern_index + 2] == "*":
                after = pattern_index + 2
                if pattern[after:after + 1] == "/":
                    if step(after + 1, text_index):
                        return True
                    cursor = text_index
                    while cursor < len(text):
                        if text[cursor] == "/" and step(after + 1, cursor + 1):
                            return True
                        cursor += 1
                    return False
                return any(step(after, cursor) for cursor in range(text_index, len(text) + 1))
            cursor = text_index
            while True:
                if step(pattern_index + 1, cursor):
                    return True
                if cursor >= len(text) or text[cursor] == "/":
                    return False
                cursor += 1
        if char == "?":
            return (text_index < len(text) and text[text_index] != "/"
                    and step(pattern_index + 1, text_index + 1))
        return (text_index < len(text) and text[text_index] == char
                and step(pattern_index + 1, text_index + 1))

    return step(0, 0)


def _subtree_size(nodes: dict[str, DirNode], path: str) -> int:
    """Count directories in a subtree, including its root."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        total += 1
        node = nodes.get(current)
        if node is not None:
            stack.extend(node.children)
    return total


def _common_ancestor(paths: Sequence[str]) -> str:
    """Return the deepest shared directory of the given relative paths."""
    if not paths:
        return "."
    parts = [path.split("/") for path in paths]
    prefix = parts[0]
    for other in parts[1:]:
        index = 0
        while index < len(prefix) and index < len(other) and prefix[index] == other[index]:
            index += 1
        prefix = prefix[:index]
    return "/".join(prefix) if prefix else "."


def _group_level(nodes: dict[str, DirNode], level: Sequence[str],
                 limit: int) -> tuple[tuple[str, ...], str, int]:
    """Split a >5 level into the largest branches plus one fallback root."""
    sizes = {path: _subtree_size(nodes, path) for path in level}
    ordered = sorted(level, key=lambda path: (-sizes[path], path))
    keep = 4 if len(ordered) >= 8 else 3
    keep = max(1, min(keep, limit - 1))
    chosen = list(ordered[:keep])
    chosen_set = set(chosen)
    remaining = [path for path in level if path not in chosen_set]
    fallback = _common_ancestor(remaining) if remaining else "."
    roots = sorted(set(chosen + [fallback]))
    return tuple(roots), fallback, keep
