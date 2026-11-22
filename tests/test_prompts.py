from __future__ import annotations

from pathlib import Path

import pytest

from egm import catalog, documents, prompts
from egm.prompts import read_context_files
from egm.types import LineRange


SAMPLE = """# Feature: Webhooks

## Overview

Deliver events to subscribers.

## Detailed Implementation Plan

### Files

- `src/webhooks.py` (Create)
"""


@pytest.fixture()
def doc(tmp_path: Path):
    path = tmp_path / "design.md"
    path.write_text(SAMPLE, encoding="utf-8")
    return documents.parse_document(SAMPLE, path)


def test_read_context_files_budget_and_truncation(tmp_path: Path):
    small = tmp_path / "small.txt"
    small.write_text("a" * 100, encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("b" * 5000, encoding="utf-8")
    files = read_context_files([str(small), str(big)], root=tmp_path,
                               budget=120, per_file=100)
    assert len(files) == 2
    assert files[0].truncated is False
    assert files[1].truncated is True
    assert sum(len(f.text) for f in files) < 1200


def test_read_context_files_skips_missing_and_huge(tmp_path: Path):
    huge = tmp_path / "huge.md"
    huge.write_text("x" * 10, encoding="utf-8")
    files = read_context_files([str(tmp_path / "nope.md"), str(huge)], root=tmp_path)
    assert [Path(f.path).name for f in files] == ["huge.md"]


def test_compose_review_contains_persona_rubric_and_numbered_slice(doc):
    spec = catalog.reviewer("design")
    bundle = prompts.compose_review(
        spec=spec, persona=catalog.persona_text("design"),
        standard_text=catalog.standard_text("feature-spec-standard"),
        doc=doc, rng=LineRange(1, 4), context_files=[],
    )
    assert "Design Critic Goldfish" in bundle.context
    assert "Feature Specification Standard" in bundle.context
    assert "1| # Feature: Webhooks" in bundle.context
    assert '"findings"' in bundle.context
    assert "Output contract" in bundle.context
    assert len(bundle.context) < prompts.HOST_CONTEXT_CAP


def test_compose_review_includes_reading_list(doc, tmp_path: Path):
    source = tmp_path / "src.py"
    source.write_text("def handler():\n    pass\n", encoding="utf-8")
    files = read_context_files([str(source)], root=tmp_path)
    bundle = prompts.compose_review(spec=catalog.reviewer("style"),
                                    persona=catalog.persona_text("style"),
                                    standard_text="", doc=doc, rng=LineRange(1, 3),
                                    context_files=files)
    assert "Reading list" in bundle.context
    assert "def handler" in bundle.context


def test_chunk_plan_covers_range_and_respects_budget(doc):
    spec = catalog.reviewer("design")
    bundles = prompts.chunk_plan(doc, doc.full_range, spec=spec,
                                 persona="P" * 2000, standard_text="S" * 1000,
                                 context_files=[], mode="review", extra="",
                                 budget=6000)
    assert len(bundles) >= 1
    assert all(b.size <= 7000 for b in bundles)
    combined = "\n".join(b.context for b in bundles)
    assert "Detailed Implementation Plan" in combined


def test_chunk_plan_brainstorm_shape(doc):
    bundle = prompts.compose_brainstorm(spec=catalog.reviewer("inventive_catalyst"),
                                        persona=catalog.persona_text("inventive_catalyst"),
                                        hypothesis="Add webhooks to the platform.",
                                        context_files=[])
    assert "Feature concept" in bundle.context
    assert "No false compromise" not in bundle.goal  # persona carries it, not the goal
    assert "BRAINSTORM" in bundle.goal


def test_goal_never_exceeds_host_limit(doc):
    spec = catalog.reviewer("design")
    bundle = prompts.compose_review(spec=spec, persona="x" * 9000,
                                    standard_text="", doc=doc, rng=doc.full_range,
                                    context_files=[], extra="y" * 9000)
    assert len(bundle.goal) <= 15_000


def test_comprehension_contract_asks_for_paraphrase_then_json(doc):
    bundle = prompts.compose_review(spec=catalog.reviewer("comprehension"),
                                    persona=catalog.persona_text("comprehension"),
                                    standard_text="", doc=doc, rng=doc.full_range,
                                    context_files=[])
    assert "## What this proposes" in bundle.context
    assert "## How the existing system works, per this document" in bundle.context
    assert bundle.context.index("What this proposes") < bundle.context.index('"findings"')
