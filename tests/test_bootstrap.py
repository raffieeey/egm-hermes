"""Tests for :mod:`egm.bootstrap`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from egm.bootstrap import (
    MAX_SOURCE_LINES_PER_FILE,
    _glob_match,
    build_plan,
    discover,
    partition,
    prune,
    verification_command,
)


def write(path: Path, text: str = "x\n") -> Path:
    """Create a file (and its parents) with deterministic content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# discover(): BFS shape, ignore rules, source accounting, limits
# --------------------------------------------------------------------------- #

def test_discover_assigns_breadth_first_depths_and_parents(tmp_path):
    write(tmp_path / "src" / "app.py", "a\nb\n")
    write(tmp_path / "src" / "pkg" / "mod.py", "x\n")
    write(tmp_path / "docs" / "guide.md", "# hi\n")

    tree = discover(tmp_path)

    assert list(tree.nodes) == [".", "docs", "src", "src/pkg"]
    assert tree.nodes["."].depth == 0
    assert tree.nodes["src"].depth == 1
    assert tree.nodes["src/pkg"].depth == 2
    assert tree.nodes["src"].parent == "."
    assert tree.nodes["src/pkg"].parent == "src"
    assert tree.nodes["."].children == ["docs", "src"]
    assert tree.nodes["src"].children == ["src/pkg"]
    assert tree.nodes["src"].source_files == ["app.py"]
    assert tree.nodes["src"].source_lines == 2
    assert tree.nodes["docs"].source_files == []          # markdown is not source
    assert tree.nodes["src"].has_manifest is False


def test_gitignore_dir_only_comments_and_nested_matches(tmp_path):
    write(tmp_path / ".gitignore", "# ignore build junk\n\ncache/\n")
    write(tmp_path / "cache" / "gen.py")
    write(tmp_path / "src" / "cache" / "gen.py")
    write(tmp_path / "src" / "keep.py")
    write(tmp_path / "src" / "artifact")                  # plain file, not a dir

    tree = discover(tmp_path)

    assert "cache" not in tree.nodes
    assert "src/cache" not in tree.nodes
    assert "cache" in tree.ignored
    assert "src/cache" in tree.ignored
    assert "src/artifact" not in tree.ignored             # dir-only rule spares files
    assert "src" in tree.nodes


def test_gitignore_leading_slash_anchors_to_root(tmp_path):
    write(tmp_path / ".gitignore", "/vendor\n")
    write(tmp_path / "vendor" / "a.py")
    write(tmp_path / "src" / "vendor" / "b.py")

    tree = discover(tmp_path)

    assert "vendor" not in tree.nodes
    assert "src/vendor" in tree.nodes


def test_gitignore_double_star_crosses_directories(tmp_path):
    write(tmp_path / ".egmignore", "**/generated\n")
    write(tmp_path / "a" / "generated" / "x.py")
    write(tmp_path / "a" / "b" / "generated" / "y.py")
    write(tmp_path / "a" / "b" / "c.py")

    tree = discover(tmp_path)

    assert "a/generated" not in tree.nodes
    assert "a/b/generated" not in tree.nodes
    assert "a/b" in tree.nodes
    assert tree.nodes["a/b"].source_files == ["c.py"]


def test_single_star_does_not_cross_directories(tmp_path):
    write(tmp_path / ".egmignore", "one/*/leaf\n")
    write(tmp_path / "one" / "two" / "leaf" / "a.py")
    write(tmp_path / "one" / "two" / "three" / "leaf" / "b.py")

    tree = discover(tmp_path)

    assert "one/two/leaf" not in tree.nodes
    assert "one/two/three/leaf" in tree.nodes


def test_question_mark_matches_one_character(tmp_path):
    write(tmp_path / ".egmignore", "dir?\n")
    write(tmp_path / "dir1" / "a.py")
    write(tmp_path / "dir12" / "b.py")

    tree = discover(tmp_path)

    assert "dir1" not in tree.nodes
    assert "dir12" in tree.nodes


def test_negation_lines_are_ignored(tmp_path):
    write(tmp_path / ".gitignore", "skipme\n!keepme\n")
    write(tmp_path / "skipme" / "a.py")
    write(tmp_path / "keepme" / "b.py")

    tree = discover(tmp_path)

    assert "skipme" not in tree.nodes
    assert "keepme" in tree.nodes


def test_egmignore_is_honoured(tmp_path):
    write(tmp_path / ".egmignore", "scratch/\n")
    write(tmp_path / "scratch" / "a.py")
    write(tmp_path / "src" / "b.py")

    tree = discover(tmp_path)

    assert "scratch" not in tree.nodes
    assert "src" in tree.nodes


def test_nested_gitignore_scopes_to_its_own_directory(tmp_path):
    write(tmp_path / "src" / ".gitignore", "local/\n")
    write(tmp_path / "src" / "local" / "a.py")
    write(tmp_path / "local" / "b.py")

    tree = discover(tmp_path)

    assert "src/local" not in tree.nodes
    assert "local" in tree.nodes


def test_explicit_ignore_list_and_ignored_record(tmp_path):
    write(tmp_path / "vendor" / "a.py")
    write(tmp_path / "src" / "b.py")

    tree = discover(tmp_path, ignore=["vendor/"])

    assert "vendor" not in tree.nodes
    assert "vendor" in tree.ignored
    assert "src" in tree.nodes


def test_hidden_defaults_and_symlinks_are_not_followed(tmp_path):
    write(tmp_path / ".cache" / "x.py")
    write(tmp_path / "node_modules" / "pkg" / "index.js")
    write(tmp_path / "dist" / "bundle.js")
    write(tmp_path / "build" / "out.py")
    write(tmp_path / "target" / "debug.rs")
    write(tmp_path / "src" / "app.py")
    write(tmp_path / "real_target" / "lib.py")
    (tmp_path / "linked").symlink_to(tmp_path / "real_target", target_is_directory=True)
    (tmp_path / "link.py").symlink_to(tmp_path / "src" / "app.py")

    tree = discover(tmp_path)

    assert set(tree.nodes) == {".", "real_target", "src"}
    assert "linked" in tree.ignored
    assert "link.py" in tree.ignored
    assert tree.nodes["src"].source_files == ["app.py"]


def test_source_extensions_and_per_file_line_cap(tmp_path):
    write(tmp_path / "a.py", "a\nb\nc\n")
    write(tmp_path / "b.md", "# not source\n")
    write(tmp_path / "c.tsx", "x\n")
    write(tmp_path / "data.json", "{}\n")
    (tmp_path / "big.go").write_text("x\n" * (MAX_SOURCE_LINES_PER_FILE + 50),
                                     encoding="utf-8")

    tree = discover(tmp_path)

    assert tree.nodes["."].source_files == ["a.py", "big.go", "c.tsx"]
    assert tree.nodes["."].source_lines == 3 + MAX_SOURCE_LINES_PER_FILE + 1
    assert MAX_SOURCE_LINES_PER_FILE == 200_000


def test_manifest_detection(tmp_path):
    write(tmp_path / "package.json", "{}")
    write(tmp_path / "svc" / "Cargo.toml", "[package]\n")
    write(tmp_path / "svc" / "main.rs")
    write(tmp_path / "dotnet" / "App.csproj", "<Project />")
    write(tmp_path / "plain" / "a.py")

    tree = discover(tmp_path)

    assert tree.nodes["."].has_manifest is True
    assert tree.nodes["svc"].has_manifest is True
    assert tree.nodes["dotnet"].has_manifest is True
    assert tree.nodes["plain"].has_manifest is False
    assert tree.nodes["svc"].source_files == ["main.rs"]


def test_max_dirs_truncates_and_records_flag(tmp_path):
    for index in range(5):
        write(tmp_path / f"d{index}" / "a.py")

    tree = discover(tmp_path, max_dirs=3)

    assert tree.truncated is True
    assert set(tree.nodes) == {".", "d0", "d1"}
    assert tree.as_dict()["truncated"] is True


def test_missing_root_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        discover(tmp_path / "does-not-exist")


# --------------------------------------------------------------------------- #
# prune(): test-only, non-code, intermediate retention, no input mutation
# --------------------------------------------------------------------------- #

def test_prune_drops_test_named_directories(tmp_path):
    write(tmp_path / "tests" / "test_app.py")
    write(tmp_path / "src" / "app.py")

    pruned = prune(discover(tmp_path))

    assert set(pruned.nodes) == {".", "src"}
    assert pruned.nodes["."].children == ["src"]


def test_prune_drops_test_only_leaf_and_strips_test_source_files(tmp_path):
    write(tmp_path / "pkg" / "helper.py", "x\n" * 4)
    write(tmp_path / "pkg" / "helper_test.py", "x\n" * 2)
    write(tmp_path / "pkg" / "thing_test.py", "x\n")
    write(tmp_path / "solo" / "WidgetTest.py", "x\n")

    pruned = prune(discover(tmp_path))

    assert "solo" not in pruned.nodes
    assert pruned.nodes["pkg"].source_files == ["helper.py"]
    assert pruned.nodes["pkg"].source_lines == 4


def test_prune_drops_non_code_and_keeps_intermediate_dirs(tmp_path):
    write(tmp_path / "src" / "deep" / "app.py")
    write(tmp_path / "assets" / "logo.svg", "<svg />\n")
    write(tmp_path / "README.md", "# hi\n")

    tree = discover(tmp_path)
    assert "assets" in tree.nodes

    pruned = prune(tree)

    assert set(pruned.nodes) == {".", "src", "src/deep"}
    assert pruned.nodes["src"].source_files == []
    assert pruned.nodes["src"].children == ["src/deep"]
    assert pruned.nodes["."].children == ["src"]
    assert pruned.nodes["."].source_files == []


def test_prune_does_not_mutate_the_input_tree(tmp_path):
    write(tmp_path / "tests" / "test_a.py")
    write(tmp_path / "src" / "a.py")
    tree = discover(tmp_path)
    before = {path: (list(node.children), list(node.source_files))
              for path, node in tree.nodes.items()}

    prune(tree)

    after = {path: (list(node.children), list(node.source_files))
             for path, node in tree.nodes.items()}
    assert before == after
    assert "tests" in tree.nodes


# --------------------------------------------------------------------------- #
# partition(): adaptive depth, >5 grouping, fallbacks, worker cap
# --------------------------------------------------------------------------- #

def _tree_with_top_level_dirs(tmp_path, names):
    for name in names:
        write(tmp_path / name / "a.py")
    return prune(discover(tmp_path))


def test_partition_uses_three_to_five_rule_at_depth_one(tmp_path):
    tree = _tree_with_top_level_dirs(tmp_path, ["a", "b", "c", "d"])

    plan = partition(tree)

    assert plan.depth == 1
    assert plan.roots == ("a", "b", "c", "d")
    assert plan.worker_count == 4
    assert "3-5" in plan.reason


def test_partition_descends_until_a_level_has_three_to_five(tmp_path):
    for parent in ("a", "b"):
        write(tmp_path / parent / "x" / "f.py")
        write(tmp_path / parent / "y" / "f.py")

    plan = partition(prune(discover(tmp_path)))

    assert plan.depth == 2
    assert plan.roots == ("a/x", "a/y", "b/x", "b/y")
    assert plan.worker_count == 4


def test_partition_groups_levels_over_five_by_subtree_size(tmp_path):
    for index in range(9):
        write(tmp_path / f"d{index}" / "a.py")
    write(tmp_path / "d0" / "x" / "y" / "z" / "f.py")

    plan = partition(prune(discover(tmp_path)))

    assert plan.depth == 1
    assert plan.roots == (".", "d0", "d1", "d2", "d3")
    assert plan.worker_count == 5
    assert "over 5" in plan.reason
    assert "fallback" in plan.reason


def test_partition_falls_back_to_root_immediate_children(tmp_path):
    write(tmp_path / "a" / "x" / "y" / "f.py")
    write(tmp_path / "b" / "g.py")

    plan = partition(prune(discover(tmp_path)))

    assert plan.depth == 1
    assert plan.roots == ("a", "b")
    assert plan.worker_count == 2
    assert "immediate children" in plan.reason


def test_partition_single_root_when_fewer_than_three_code_dirs(tmp_path):
    write(tmp_path / "a" / "x" / "f.py")

    plan = partition(prune(discover(tmp_path)))

    assert plan.roots == (".",)
    assert plan.depth == 0
    assert plan.worker_count == 1


def test_partition_respects_max_workers(tmp_path):
    tree = _tree_with_top_level_dirs(tmp_path, ["a", "b", "c", "d", "e"])

    plan = partition(tree, max_workers=3)

    assert plan.roots == ("a", "b", "c")
    assert plan.worker_count == 3
    assert "Capped at max_workers=3" in plan.reason


def test_partition_single_root_for_empty_tree(tmp_path):
    plan = partition(prune(discover(tmp_path)))

    assert plan.roots == (".",)
    assert plan.depth == 0
    assert plan.worker_count == 1


# --------------------------------------------------------------------------- #
# verification_command(): every ecosystem and the priority order
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("BUILD.bazel", "bazel test //..."),
        ("pnpm-lock.yaml", "pnpm test"),
        ("yarn.lock", "yarn test"),
        ("package.json", "npm test"),
        ("pyproject.toml", "pytest"),
        ("pytest.ini", "pytest"),
        ("setup.py", "pytest"),
        ("Cargo.toml", "cargo test"),
        ("go.mod", "go test ./..."),
        ("Makefile", "make test"),
    ],
)
def test_verification_command_for_each_ecosystem(tmp_path, filename, expected):
    write(tmp_path / filename)

    assert verification_command(tmp_path) == expected


@pytest.mark.parametrize("filename", ["App.sln", "App.slnx", "App.csproj"])
def test_verification_command_for_dotnet(tmp_path, filename):
    write(tmp_path / filename)

    assert verification_command(tmp_path) == "dotnet test"


@pytest.mark.parametrize(
    ("filenames", "expected"),
    [
        (["BUILD", "package.json"], "bazel test //..."),
        (["pnpm-lock.yaml", "package.json"], "pnpm test"),
        (["yarn.lock", "package.json"], "yarn test"),
        (["pyproject.toml", "Cargo.toml"], "pytest"),
        (["Cargo.toml", "go.mod"], "cargo test"),
        (["go.mod", "Makefile"], "go test ./..."),
        (["App.csproj", "Makefile"], "dotnet test"),
    ],
)
def test_verification_command_priority(tmp_path, filenames, expected):
    for filename in filenames:
        write(tmp_path / filename)

    assert verification_command(tmp_path) == expected


def test_verification_command_fallback(tmp_path):
    assert verification_command(tmp_path) == "<test-command>"
    assert verification_command(tmp_path / "missing") == "<test-command>"


# --------------------------------------------------------------------------- #
# build_plan(): JSON shape, safety gate, determinism
# --------------------------------------------------------------------------- #

def _plan_tree(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\n")
    write(tmp_path / "src" / "app.py", "x\n" * 3)
    write(tmp_path / "src" / "pkg" / "mod.py")
    write(tmp_path / "lib" / "util.py")
    write(tmp_path / "tools" / "run.py")
    write(tmp_path / "tests" / "test_src.py")


def test_build_plan_json_shape(tmp_path):
    _plan_tree(tmp_path)

    plan = build_plan(tmp_path, target_filename="EGM.md", max_workers=4)

    assert plan["root"] == str(tmp_path)
    assert plan["target_filename"] == "EGM.md"
    assert plan["verification_command"] == "pytest"
    assert plan["directory_count"] == 5
    assert plan["max_depth"] == 2
    assert plan["code_directories"] == ["lib", "src", "src/pkg", "tools"]
    assert plan["needs_safety_gate"] is False
    assert plan["partitions"] == {
        "roots": ["lib", "src", "tools"],
        "depth": 1,
        "reason": plan["partitions"]["reason"],
        "worker_count": 3,
    }
    assert [node["path"] for node in plan["nodes"]] == [
        ".", "lib", "src", "src/pkg", "tools"]

    root = plan["nodes"][0]
    assert root["children"] == ["lib", "src", "tools"]
    assert root["source_files"] == []
    assert root["has_manifest"] is True
    assert root["test_only"] is False
    for node in plan["nodes"]:
        assert set(node) == {
            "path", "depth", "children", "source_files", "source_lines",
            "has_manifest", "test_only", "summary"}

    src = plan["nodes"][2]
    assert src["children"] == ["src/pkg"]
    assert src["source_files"] == ["app.py"]
    assert src["source_lines"] == 3
    assert src["summary"] == "1 source file (3 lines); 1 child directory"
    json.dumps(plan)  # must be JSON-serialisable


def test_build_plan_passes_ignore_patterns_through(tmp_path):
    write(tmp_path / "src" / "app.py")
    write(tmp_path / "generated" / "code.py")

    plan = build_plan(tmp_path, ignore=["generated/"])

    assert [node["path"] for node in plan["nodes"]] == [".", "src"]
    assert plan["directory_count"] == 2
    assert plan["target_filename"] == "README.md"


def test_build_plan_safety_gate_threshold(tmp_path):
    for index in range(16):
        write(tmp_path / "big" / f"d{index}" / "a.py")
    for index in range(14):
        write(tmp_path / "small" / f"d{index}" / "a.py")

    big = build_plan(tmp_path / "big")
    small = build_plan(tmp_path / "small")

    assert big["directory_count"] == 17
    assert big["needs_safety_gate"] is True
    assert small["directory_count"] == 15
    assert small["needs_safety_gate"] is False


def test_build_plan_is_deterministic(tmp_path):
    _plan_tree(tmp_path)

    first = build_plan(tmp_path)
    second = build_plan(tmp_path)

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_tree_as_dict_shape(tmp_path):
    write(tmp_path / "src" / "app.py")
    tree = discover(tmp_path)

    data = tree.as_dict()

    assert data["root"] == str(tmp_path)
    assert data["directory_count"] == 2
    assert data["max_depth"] == 1
    assert data["truncated"] is False
    assert data["code_dirs"] == ["src"]
    assert [node["path"] for node in data["nodes"]] == [".", "src"]


# --------------------------------------------------------------------------- #
# Glob subset: `*` never crosses `/`, `**` always does, `?` is one character
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("*.py", "a.py", True),
        ("*.py", "sub/a.py", False),
        ("**/*.py", "sub/a.py", True),
        ("**/*.py", "a.py", True),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/*/b", "a/x/b", True),
        ("a/*/b", "a/x/y/b", False),
        ("dir?", "dir1", True),
        ("dir?", "dir12", False),
        ("d?r", "d/r", False),
        ("lit", "lit", True),
        ("lit", "Lit", False),
    ],
)
def test_glob_subset(pattern, text, expected):
    assert _glob_match(pattern, text) is expected
