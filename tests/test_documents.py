"""Tests for egm.documents: parsing, split discovery, links and chunking."""

from __future__ import annotations

from pathlib import Path

import pytest

from egm.documents import (
    LinkedFiles,
    discover_split,
    linked_files,
    parse_document,
    read_document,
    split_by_heading,
)
from egm.types import DocumentError, LineRange


# --------------------------------------------------------------------------- parsing


def test_headings_inside_backtick_fences_are_ignored() -> None:
    text = (
        "# Feature\n"
        "\n"
        "```python\n"
        "## Not A Heading\n"
        "# Also Not\n"
        "```\n"
        "\n"
        "## Implementation Plan\n"
        "\n"
        "Steps.\n"
    )
    doc = parse_document(text)
    assert [section.title for section in doc.sections] == ["Feature", "Implementation Plan"]


def test_tilde_and_longer_fences_close_on_matching_marker() -> None:
    text = (
        "# Top\n"
        "~~~~\n"
        "## Hidden By Tilde\n"
        "~~~~\n"
        "## Real\n"
        "```\n"
        "##### Hidden By Backtick\n"
        "````\n"
        "## After\n"
    )
    doc = parse_document(text)
    assert [section.title for section in doc.sections] == ["Top", "Real", "After"]


def test_unclosed_fence_hides_every_trailing_heading() -> None:
    doc = parse_document("# Top\n```\n## Plan\n")
    assert [section.title for section in doc.sections] == ["Top"]


def test_atx_heading_requires_space_and_strips_closing_hashes() -> None:
    doc = parse_document("#NotAHeading\n## Real Heading ##\n")
    assert [(section.level, section.title) for section in doc.sections] == [(2, "Real Heading")]


def test_section_ends_before_next_same_or_higher_heading() -> None:
    text = "# Doc\n## A\nalpha\n### A1\na1\n## B\nbeta\n"
    doc = parse_document(text)
    spans = {section.title: (section.start, section.end) for section in doc.sections}
    assert spans == {"Doc": (1, 7), "A": (2, 5), "A1": (4, 5), "B": (6, 7)}


def test_duplicate_heading_titles_are_distinct_sections() -> None:
    doc = parse_document("## Notes\none\n## Notes\ntwo\n")
    notes = [section for section in doc.sections if section.title == "Notes"]
    assert [(section.start, section.end) for section in notes] == [(1, 2), (3, 4)]
    assert len(doc.find_sections("Notes")) == 2


# --------------------------------------------------------------------- split discovery


@pytest.mark.parametrize(
    "title",
    [
        "Detailed Implementation Plan",
        "Implementation Plan",
        "WORK BREAKDOWN",
        "Implementation Steps",
        "Implementation Plan for Webhooks",
    ],
)
def test_discover_split_recognizes_plan_headings(title: str) -> None:
    doc = parse_document(f"# Feature\nintro\n## {title}\nstep\n")
    split = discover_split(doc)
    assert split.boundary_line == 3
    assert split.boundary_title == title
    assert split.feature == LineRange(1, 2)
    assert split.detailed == LineRange(3, 4)


def test_discover_split_boundary_on_first_line_keeps_nonempty_feature() -> None:
    split = discover_split(parse_document("## Implementation Plan\nstep\n"))
    assert split.feature == LineRange(1, 1)
    assert split.detailed == LineRange(1, 2)
    assert split.boundary_line == 1


def test_discover_split_reports_ambiguity_and_falls_back_to_full_feature() -> None:
    doc = parse_document("# Feature\n\n## Implementation Plan\n\n## Work Breakdown\n")
    split = discover_split(doc)
    assert split.feature == doc.full_range
    assert split.detailed is None
    assert split.boundary_line == 3
    assert split.boundary_title == "Implementation Plan"
    assert [section.title for section in split.ambiguous] == [
        "Implementation Plan",
        "Work Breakdown",
    ]


def test_discover_split_without_plan_covers_whole_document() -> None:
    doc = parse_document("# Feature\ntext\n")
    split = discover_split(doc)
    assert split.feature == LineRange(1, 2)
    assert split.detailed is None
    assert split.boundary_line is None
    assert split.boundary_title == ""
    assert split.ambiguous == ()


def test_discover_split_ignores_plan_headings_inside_fences() -> None:
    doc = parse_document("# Feature\n```\n## Implementation Plan\n```\n")
    split = discover_split(doc)
    assert split.feature == doc.full_range
    assert split.detailed is None


# ------------------------------------------------------------------- semantic resolve


def test_resolve_full_file_and_star() -> None:
    doc = parse_document("# A\none\n## B\ntwo\n")
    assert doc.resolve("full file") == LineRange(1, 4)
    assert doc.resolve("*") == LineRange(1, 4)


def test_resolve_explicit_line_range() -> None:
    doc = parse_document("\n".join(f"line {number}" for number in range(1, 41)))
    assert doc.resolve("L10-L40") == LineRange(10, 40)


def test_resolve_clamps_range_to_document_length() -> None:
    doc = parse_document("\n".join(f"line {number}" for number in range(1, 11)))
    assert doc.resolve("L10-L400") == LineRange(10, 10)


def test_resolve_heading_with_and_without_hashes() -> None:
    doc = parse_document("# Spec\nbody\n## Implementation Plan\nstep\n")
    assert doc.resolve("## Implementation Plan") == LineRange(3, 4)
    assert doc.resolve("Implementation Plan") == LineRange(3, 4)


def test_resolve_section_id_token_matches_heading() -> None:
    doc = parse_document("# Spec\n\n### S1.12 Retry Semantics\nbody\n")
    assert [section.title for section in doc.find_sections("S1.12")] == [
        "S1.12 Retry Semantics"
    ]
    assert doc.resolve("S1.12 Retry Semantics") == LineRange(3, 4)


def test_resolve_ambiguous_heading_raises_document_error() -> None:
    doc = parse_document("## Notes\na\n## Notes\nb\n")
    with pytest.raises(DocumentError, match="matches 2 sections"):
        doc.resolve("Notes")


def test_resolve_quoted_phrase_inside_instruction() -> None:
    doc = parse_document("# Spec\nintro\n## Concurrency Model\nbody\n## Other\nx\n")
    assert doc.resolve('Review the "Concurrency Model" section') == LineRange(3, 4)


# ---------------------------------------------------------------------- linked files


@pytest.fixture()
def link_tree(tmp_path):
    root = tmp_path / "root"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (root / "top.md").write_text("# Top\n", encoding="utf-8")
    (docs / "local.md").write_text("# Local\n", encoding="utf-8")
    (docs / "my file.md").write_text("# Spaced\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")
    design = docs / "design.md"
    design.write_text("# Design\n", encoding="utf-8")
    return tmp_path, root, design


def test_linked_files_classifies_inside_outside_missing_and_urls(link_tree) -> None:
    tmp_path, root, design = link_tree
    far = str(Path("..") / ".." / "outside.md")
    text = "\n".join(
        [
            "[local](local.md)",
            "[up](../top.md)",
            "[gone](../missing.md)",
            f"[far]({far})",
            "[web](https://example.com/page)",
            "[mail](mailto:team@example.com)",
            "[anchor](#section)",
            "",
        ]
    )
    found = linked_files(parse_document(text, design), root)
    assert found.included == (
        (design.parent / "local.md").resolve(),
        (root / "top.md").resolve(),
    )
    assert found.missing == ("../missing.md",)
    assert found.outside == (far,)
    assert found.urls == ("https://example.com/page", "mailto:team@example.com")


def test_linked_files_dedupes_and_handles_titles_autolinks_and_encoding(link_tree) -> None:
    tmp_path, root, design = link_tree
    text = "\n".join(
        [
            '[one](local.md "Local file")',
            "<local.md>",
            "[spaced](my%20file.md)",
            "[again](./local.md)",
            "",
        ]
    )
    found = linked_files(parse_document(text, design), root)
    assert found.included == (
        (design.parent / "local.md").resolve(),
        (design.parent / "my file.md").resolve(),
    )


def test_linked_files_treats_symlink_escape_as_outside(link_tree) -> None:
    tmp_path, root, design = link_tree
    link = design.parent / "escape.md"
    try:
        link.symlink_to(tmp_path / "outside.md")
    except OSError:  # pragma: no cover - platform without symlink support
        pytest.skip("symlinks unavailable")
    found = linked_files(parse_document("[escape](escape.md)\n", design), root)
    assert found.included == ()
    assert found.outside == ("escape.md",)


def test_linked_files_skips_fenced_links_anchors_and_empty_targets(link_tree) -> None:
    tmp_path, root, design = link_tree
    text = "\n".join(
        [
            "```",
            "[fenced](local.md)",
            "```",
            "[anchor](#top)",
            "[empty]()",
            "[anchor only](#other)",
            "",
        ]
    )
    found = linked_files(parse_document(text, design), root)
    assert found.included == ()
    assert found.missing == ()
    assert found.outside == ()
    assert found.urls == ()


def test_linked_files_drops_fragments_and_accepts_angle_destinations(link_tree) -> None:
    tmp_path, root, design = link_tree
    text = "\n".join(
        [
            "[frag](local.md#details)",
            "[spaced](<my file.md>)",
            "<https://example.com/docs>",
            "",
        ]
    )
    found = linked_files(parse_document(text, design), root)
    assert found.included == (
        (design.parent / "local.md").resolve(),
        (design.parent / "my file.md").resolve(),
    )
    assert found.urls == ("https://example.com/docs",)


def test_linked_files_marks_nonexistent_escape_as_outside(link_tree) -> None:
    tmp_path, root, design = link_tree
    escape = str(Path("..") / ".." / "missing-outside.md")
    found = linked_files(parse_document(f"[escape]({escape})\n", design), root)
    assert found.outside == (escape,)
    assert found.missing == ()


def test_linked_files_as_dict_returns_string_lists(link_tree) -> None:
    tmp_path, root, design = link_tree
    found = linked_files(
        parse_document("[local](local.md)\n[web](https://example.com)\n", design), root
    )
    data = found.as_dict()
    assert data["included"] == [str((design.parent / "local.md").resolve())]
    assert data["urls"] == ["https://example.com"]
    assert all(isinstance(values, list) for values in data.values())
    assert isinstance(found, LinkedFiles)


# ------------------------------------------------------------------------ chunking

SPLIT_DOC = "# Doc\n\nalpha\nbeta\n## Section\ngamma\ndelta\n"


def test_split_by_heading_prefers_heading_start_cuts() -> None:
    doc = parse_document(SPLIT_DOC)
    chunks = split_by_heading(doc, doc.full_range, max_chars=30)
    assert chunks == [LineRange(1, 4), LineRange(5, 7)]


@pytest.mark.parametrize("max_chars", [1, 7, 12, 25, 40, 500])
def test_split_by_heading_covers_range_exactly(max_chars: int) -> None:
    doc = parse_document(SPLIT_DOC + "\nmore text\n")
    chunks = split_by_heading(doc, doc.full_range, max_chars=max_chars)
    assert chunks[0].start == 1
    assert chunks[-1].end == doc.line_count
    assert all(chunk.start <= chunk.end for chunk in chunks)
    for left, right in zip(chunks, chunks[1:]):
        assert left.end + 1 == right.start
    rebuilt = "\n".join("\n".join(doc.lines[chunk.start - 1:chunk.end]) for chunk in chunks)
    assert rebuilt == "\n".join(doc.lines)


@pytest.mark.parametrize("max_chars", [10, 25, 40])
def test_split_by_heading_respects_character_budget(max_chars: int) -> None:
    doc = parse_document(SPLIT_DOC)
    for chunk in split_by_heading(doc, doc.full_range, max_chars=max_chars):
        text = "\n".join(doc.lines[chunk.start - 1:chunk.end])
        assert len(text) <= max_chars or chunk.length == 1


def test_split_by_heading_hard_cuts_when_no_heading_fits() -> None:
    doc = parse_document("x" * 100 + "\n" + "y" * 100 + "\n")
    assert split_by_heading(doc, doc.full_range, max_chars=50) == [
        LineRange(1, 1),
        LineRange(2, 2),
    ]


def test_split_by_heading_covers_only_requested_range() -> None:
    doc = parse_document("# A\n" + "line\n" * 20 + "## B\n" + "tail\n" * 5)
    chunks = split_by_heading(doc, LineRange(5, 15), max_chars=20)
    assert chunks[0].start == 5
    assert chunks[-1].end == 15
    for left, right in zip(chunks, chunks[1:]):
        assert left.end + 1 == right.start


def test_split_by_heading_rejects_nonpositive_budget() -> None:
    doc = parse_document(SPLIT_DOC)
    with pytest.raises(ValueError):
        split_by_heading(doc, doc.full_range, max_chars=0)


# ---------------------------------------------------------------------- read_document


def test_read_document_reads_utf8_file(tmp_path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("# Title\nbody\n", encoding="utf-8")
    doc = read_document(path)
    assert doc.path == path
    assert doc.line_count == 2
    assert doc.sections[0].title == "Title"


def test_read_document_strips_utf8_bom(tmp_path) -> None:
    path = tmp_path / "bom.md"
    path.write_bytes("\ufeff# Title\nbody\n".encode("utf-8"))
    doc = read_document(path)
    assert doc.sections[0].title == "Title"


def test_read_document_missing_file_raises_document_error(tmp_path) -> None:
    with pytest.raises(DocumentError):
        read_document(tmp_path / "nope.md")


def test_read_document_non_utf8_file_raises_document_error(tmp_path) -> None:
    path = tmp_path / "bad.md"
    path.write_bytes(b"# Bad\n\xff\xfe\n")
    with pytest.raises(DocumentError):
        read_document(path)
