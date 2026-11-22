from __future__ import annotations

import pytest

from egm.documents import parse_document
from egm.types import DocumentError, LineRange

DOC = """# Orders

## Overview

Takes orders.

### S1.12 Order intake

Validates the payload.

### S1.13 Order storage

Writes rows.

## Detailed Implementation Plan

### Files

- `src/orders.py` (Create)
"""


@pytest.fixture()
def doc():
    return parse_document(DOC, "orders.md")


def test_full_file_aliases(doc):
    for value in ("", "full file", "full", "*", "whole file"):
        assert doc.resolve(value).start == 1
        assert doc.resolve(value).end == doc.line_count


def test_explicit_ranges(doc):
    assert doc.resolve("L3-L6") == LineRange(3, 6)
    assert doc.resolve("3-6") == LineRange(3, 6)
    assert doc.resolve("lines 3..6") == LineRange(3, 6)
    assert doc.resolve("7") == LineRange(7, 7)


def test_identifier_targets_resolve_to_sections(doc):
    """The SS-ID case: S1.12 must never be read as 'lines 1 to 12'."""
    resolved = doc.resolve("S1.12")
    assert resolved.start == doc.find_sections("S1.12")[0].start
    assert resolved.start != 1
    assert doc.resolve("#S1.12") == resolved


def test_quoted_phrase_inside_instruction(doc):
    resolved = doc.resolve("Target the H4 block with SS-ID 'S1.13'")
    assert resolved.start == doc.find_sections("S1.13")[0].start


def test_heading_title_and_hashes(doc):
    assert doc.resolve("## Overview").start == doc.find_sections("Overview")[0].start
    assert doc.resolve("Detailed Implementation Plan").start == doc.find_sections(
        "Detailed Implementation Plan")[0].start


def test_ambiguous_heading_raises_with_candidates(doc):
    with pytest.raises(DocumentError) as exc:
        doc.resolve("S1.1")
    assert "matches" in str(exc.value)


def test_unknown_target_raises(doc):
    with pytest.raises(DocumentError):
        doc.resolve("X9.99")


def test_range_is_clamped_to_document(doc):
    assert doc.resolve("L1-L9999").end == doc.line_count
