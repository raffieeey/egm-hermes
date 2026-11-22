"""Tests for ``egm.synth``: payload parsing, normalization and report layouts."""

from __future__ import annotations

import json
import re

import pytest

from egm.synth import (
    counts,
    dedupe,
    merge_stage_findings,
    normalize,
    parse_findings,
    render_consolidated,
    render_review_report,
)
from egm.types import Finding, LineRange, ReviewerOutcome, ReviewerSpec, Split, StageReport


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_spec(name: str = "design", role: str = "Design Critic") -> ReviewerSpec:
    return ReviewerSpec(name=name, persona=name, standard=None, role=role)


def make_outcome(name: str = "design", status: str = "completed", raw: str = "",
                 findings: tuple[Finding, ...] = ()) -> ReviewerOutcome:
    return ReviewerOutcome(spec=make_spec(name), status=status, findings=findings, raw=raw)


def make_finding(description: str, *, severity: str = "major", reviewer: str = "design",
                 scope: str = "L1-L2", rationale: str = "", suggestion: str = "",
                 finding_id: str = "") -> Finding:
    return Finding(reviewer=reviewer, severity=severity, scope=scope,
                   description=description, rationale=rationale,
                   suggestion=suggestion, id=finding_id)


def make_stage(number: int, findings: tuple[Finding, ...] = (),
               outcomes: tuple[ReviewerOutcome, ...] = (), *, key: str | None = None,
               title: str | None = None, path: str | None = None,
               skipped: bool = False) -> StageReport:
    return StageReport(stage=number, key=key or f"stage_{number}",
                       title=title or f"Stage {number} Review",
                       path=path or f"stage_{number}_report.md",
                       outcomes=outcomes, findings=findings, skipped=skipped)


# --------------------------------------------------------------------------- #
# parse_findings: payload tolerance
# --------------------------------------------------------------------------- #

def test_parse_bare_array() -> None:
    payload = json.dumps([{
        "severity": "blocker", "scope": "L3-L9",
        "description": "Missing rollback plan.",
        "rationale": "A failed migration strands the schema.",
        "suggestion": "Add a rollback section.",
    }])
    findings = parse_findings(payload, reviewer="design")
    assert len(findings) == 1
    assert findings[0].severity == "blocker"
    assert findings[0].scope == "L3-L9"
    assert findings[0].id == "design-r1-1"


def test_parse_object_with_findings_and_default_severity() -> None:
    payload = json.dumps({"findings": [
        {"description": "First issue."},
        {"description": "Second issue.", "severity": "nit"},
    ]})
    findings = parse_findings(payload, reviewer="style")
    assert [f.description for f in findings] == ["First issue.", "Second issue."]
    assert findings[0].severity == "major"  # missing severity defaults to major
    assert findings[1].severity == "nit"
    assert findings[0].id == "style-r1-1"
    assert findings[1].id == "style-r1-2"


def test_parse_fenced_json_block() -> None:
    payload = (
        "I read the document twice.\n\n"
        "```json\n"
        '{"findings": [{"description": "Vague success metric.", "severity": "high"},'
        ' {"description": "Missing owner.", "severity": "minor"}]}\n'
        "```\n\n"
        "Let me know if you want more detail.\n"
    )
    findings = parse_findings(payload, reviewer="design")
    assert len(findings) == 2
    assert findings[0].severity == "major"
    assert findings[1].severity == "nit"


def test_parse_prose_wrapped_array() -> None:
    payload = (
        "Here are my findings.\n\n"
        '[{"description": "No acceptance criteria.", "scope": "L4-L8"},'
        ' {"description": "Undefined error path.", "severity": "blocker"}]\n\n'
        "That is all.\n"
    )
    findings = parse_findings(payload, reviewer="comprehension")
    assert len(findings) == 2
    assert {f.id for f in findings} == {"comprehension-r1-1", "comprehension-r1-2"}


def test_parse_nested_report_object() -> None:
    payload = json.dumps({"report": {"target": "doc.md", "findings": [
        {"issue": "Ambiguous data flow.", "severity": "critical"}]}})
    findings = parse_findings(payload, reviewer="structural")
    assert len(findings) == 1
    assert findings[0].description == "Ambiguous data flow."
    assert findings[0].severity == "blocker"


def test_parse_single_finding_object() -> None:
    payload = '{"severity": "nit", "description": "Minor wording issue.", "scope": "L2"}'
    findings = parse_findings(payload, reviewer="style")
    assert len(findings) == 1
    assert findings[0].severity == "nit"
    assert findings[0].id == "style-r1-1"


def test_parse_single_finding_with_finding_key() -> None:
    payload = json.dumps({"finding": "The diagram contradicts the prose.",
                          "recommendation": "Redraw it.", "proposed_edit": "Fix arrow."})
    findings = parse_findings(payload, reviewer="design")
    assert findings[0].description == "The diagram contradicts the prose."
    assert findings[0].rationale == "Redraw it."
    assert findings[0].suggestion == "Fix arrow."


def test_parse_skips_decoy_object_before_array() -> None:
    payload = 'Metadata: {"count": 1}\nFindings:\n[{"description": "One."}, {"description": "Two."}]'
    findings = parse_findings(payload, reviewer="zoom_out")
    assert [f.description for f in findings] == ["One.", "Two."]


def test_parse_jsonl_objects_in_prose() -> None:
    payload = ('1. {"description": "First issue.", "severity": "blocker"}\n'
               '2. {"description": "Second issue.", "severity": "nit"}\n')
    findings = parse_findings(payload, reviewer="design")
    assert [f.description for f in findings] == ["First issue.", "Second issue."]


def test_parse_salvages_truncated_array() -> None:
    payload = 'Findings:\n[{"description": "Alpha."}, {"description": "Beta."}, {"description": "Gam'
    findings = parse_findings(payload, reviewer="style")
    assert [f.description for f in findings] == ["Alpha.", "Beta."]


def test_parse_tolerates_trailing_commas() -> None:
    payload = '[{"description": "Trailing comma.", "severity": "critical",},]'
    findings = parse_findings(payload, reviewer="design")
    assert findings[0].severity == "blocker"


def test_parse_invalid_payload_raises() -> None:
    with pytest.raises(ValueError, match="No usable findings"):
        parse_findings("I found no issues in this document at all.", reviewer="design")


def test_parse_empty_payload_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_findings("   \n\t ", reviewer="design")


def test_parse_tolerates_capitalized_keys() -> None:
    payload = json.dumps({"Findings": [{"Description": "Broken link.", "Severity": "Blocker"}]})
    findings = parse_findings(payload, reviewer="style")
    assert findings[0].description == "Broken link."
    assert findings[0].severity == "blocker"


def test_parse_dedupes_repeated_findings() -> None:
    payload = json.dumps([
        {"description": "The retry budget is never defined for the worker pool.",
         "rationale": "Operators cannot size the queue."},
        {"description": "The retry budget is never defined for the worker pool queue.",
         "suggestion": "State the budget."},
    ])
    findings = parse_findings(payload, reviewer="design")
    assert len(findings) == 1
    assert "Operators cannot size the queue." in findings[0].rationale
    assert findings[0].suggestion == "State the budget."


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #

def test_normalize_severity_aliases() -> None:
    aliases = {
        "Critical finding.": "blocker",
        "P0 finding.": "blocker",
        "High finding.": "major",
        "Minor finding.": "nit",
        "Polish finding.": "nit",
        "Low finding.": "nit",
        "Unclassified finding.": "major",
    }
    findings = [make_finding(description, severity=severity)
                for description, severity in zip(
                    aliases,
                    ["Critical", "P0", "High", "Minor", "Polish", "Low", "???"],
                    strict=True)]
    normalized = normalize(findings, reviewer="design")
    assert {f.description: f.severity for f in normalized} == aliases


def test_normalize_orders_by_severity_then_original_order() -> None:
    findings = [
        make_finding("Nit first.", severity="nit"),
        make_finding("Blocker first.", severity="blocker"),
        make_finding("Major middle.", severity="major"),
        make_finding("Blocker second.", severity="blocker"),
    ]
    normalized = normalize(findings, reviewer="design")
    assert [f.description for f in normalized] == [
        "Blocker first.", "Blocker second.", "Major middle.", "Nit first."]
    assert [f.id for f in normalized] == [
        "design-r1-1", "design-r1-2", "design-r1-3", "design-r1-4"]


def test_normalize_is_stable_and_honours_round() -> None:
    findings = [make_finding("Only finding.", severity="major")]
    first = normalize(findings, reviewer="design", round_no=3)
    second = normalize(findings, reviewer="design", round_no=3)
    assert [f.id for f in first] == [f.id for f in second] == ["design-r3-1"]
    assert normalize(findings, reviewer="design")[0].id == "design-r1-1"


def test_normalize_drops_empty_descriptions_and_caps_text() -> None:
    long_text = "a " * 2500  # 5000 characters
    findings = [
        make_finding("   "),
        make_finding(""),
        make_finding("Real finding.", rationale=long_text, suggestion="Fix it."),
    ]
    normalized = normalize(findings, reviewer="design")
    assert len(normalized) == 1
    assert 3900 < len(normalized[0].rationale) <= 4000
    assert normalized[0].suggestion == "Fix it."


def test_normalize_strips_and_collapses_whitespace() -> None:
    findings = [make_finding("  Fix   this\nline  ", scope=" L4 - L9 ")]
    normalized = normalize(findings, reviewer="design")
    assert normalized[0].description == "Fix this line"
    assert normalized[0].scope == "L4 - L9"


def test_normalize_does_not_mutate_inputs() -> None:
    original = make_finding("  Untouched  ", severity="Blocker")
    normalize([original], reviewer="design")
    assert original.description == "  Untouched  "
    assert original.severity == "Blocker"
    assert original.id == ""


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #

def test_dedupe_keeps_highest_severity_and_merges_text() -> None:
    findings = [
        make_finding("Missing retry budget for the worker pool.", severity="major",
                     rationale="Operators cannot size the queue."),
        make_finding("Missing retry budget for the worker queue pool.", severity="blocker",
                     rationale="Retries amplify the outage.", suggestion="State the budget."),
    ]
    deduped = dedupe(findings)
    assert len(deduped) == 1
    assert deduped[0].severity == "blocker"
    assert deduped[0].rationale == ("Operators cannot size the queue. | "
                                    "Retries amplify the outage.")
    assert deduped[0].suggestion == "State the budget."


def test_dedupe_respects_reviewer_and_scope() -> None:
    text = "The retry budget is never defined for the worker pool."
    findings = [
        make_finding(text, reviewer="design", scope="L1-L5"),
        make_finding(text, reviewer="style", scope="L1-L5"),
        make_finding(text, reviewer="design", scope="L20-L30"),
    ]
    assert len(dedupe(findings)) == 3


def test_dedupe_normalizes_scope_ranges() -> None:
    findings = [
        make_finding("The retry budget is never defined for the worker pool.",
                     scope="L10-L20", severity="nit"),
        make_finding("The retry budget is never defined for the worker pool.",
                     scope="lines 10-20", severity="major"),
    ]
    deduped = dedupe(findings)
    assert len(deduped) == 1
    assert deduped[0].severity == "major"


def test_dedupe_keeps_unrelated_findings() -> None:
    findings = [
        make_finding("Missing owner for the rollout step.", scope="L1-L5"),
        make_finding("No acceptance criteria anywhere in the plan.", scope="L1-L5"),
    ]
    assert len(dedupe(findings)) == 2


# --------------------------------------------------------------------------- #
# counts
# --------------------------------------------------------------------------- #

def test_counts_includes_all_severities() -> None:
    result = counts([
        make_finding("A.", severity="blocker"),
        make_finding("B.", severity="major"),
        make_finding("C.", severity="major"),
    ])
    assert result == {"blocker": 1, "major": 2, "nit": 0}


# --------------------------------------------------------------------------- #
# render_review_report
# --------------------------------------------------------------------------- #

def test_render_review_report_exact_layout() -> None:
    outcomes = [
        make_outcome("design", "completed", raw="DESIGN RAW"),
        make_outcome("style", "not run"),
    ]
    findings = [make_finding(
        "The plan never says how to roll back a failed migration.",
        severity="blocker", scope="L10-L20",
        rationale="A failed migration would leave the schema half-applied.",
        suggestion="Add a rollback section.")]
    report = render_review_report(
        title="Goldfish Review Report", target="docs/design.md",
        line_range=LineRange(10, 20), outcomes=outcomes, findings=findings)
    expected = """# Goldfish Review Report

**Target:** `docs/design.md` (`L10-L20`)

## Reviewer Status

| Reviewer | Status |
| --- | --- |
| `design` | completed |
| `style` | not run |

## Prioritized Findings (Blockers -> Major -> Nits)

### The plan never says how to roll back a failed migration - Severity: 🔴 Blocker
* **Reviewer Lens:** `design`
* **Affected Scope:** `L10-L20`
* **Discrepancy Description:** The plan never says how to roll back a failed migration.
* **Rationale & Recommendation:** A failed migration would leave the schema half-applied.
* **Proposed Edit:** Add a rollback section.

## Appendix: Raw Reviewer Reports
<details><summary>design</summary>

DESIGN RAW

</details>
"""
    assert report == expected


def test_render_review_report_escapes_pipes_in_tables() -> None:
    outcomes = [make_outcome("a|b", "completed")]
    report = render_review_report(title="T", target="doc.md", line_range=None,
                                  outcomes=outcomes, findings=[])
    assert "| `a\\|b` | completed |" in report


def test_render_review_report_empty_findings_has_no_appendix() -> None:
    outcomes = [make_outcome("design", "completed", raw="RAW TEXT")]
    report = render_review_report(title="T", target="doc.md", line_range=None,
                                  outcomes=outcomes, findings=[])
    assert "## Findings\n\nNo findings." in report
    assert "## Prioritized Findings" not in report
    assert "<details>" not in report


def test_render_review_report_status_table_lists_not_run_and_appendix() -> None:
    outcomes = [
        make_outcome("design", "completed", raw="DESIGN RAW"),
        make_outcome("style", "failed", raw=""),
        make_outcome("structural", "timeout", raw="STRUCTURAL RAW"),
    ]
    report = render_review_report(title="T", target="doc.md", line_range=None,
                                  outcomes=outcomes,
                                  findings=[make_finding("One issue.")])
    assert "| `design` | completed |" in report
    assert "| `style` | not run |" in report
    assert "| `structural` | not run |" in report
    assert "<details><summary>design</summary>" in report
    assert "<details><summary>structural</summary>" in report
    assert "<details><summary>style</summary>" not in report


def test_render_review_report_notes_only_when_body_present() -> None:
    base = dict(title="T", target="doc.md", line_range=None,
                outcomes=[make_outcome("design", "completed")],
                findings=[make_finding("One issue.")])
    assert "## Notes" not in render_review_report(**base)
    with_notes = render_review_report(**base, body="Reviewed against v2 of the spec.\n\nSecond line.")
    assert "## Notes\n\nReviewed against v2 of the spec.\n\nSecond line." in with_notes


def test_render_review_report_short_title_rules() -> None:
    long_description = ("This sentence is deliberately verbose and keeps going well past the "
                        "seventy-two character limit so the title must be truncated. Second one.")
    report = render_review_report(
        title="T", target="doc.md", line_range=None,
        outcomes=[make_outcome("design", "completed")],
        findings=[make_finding(long_description, severity="nit")])
    title_line = next(line for line in report.splitlines() if line.startswith("### "))
    title = title_line.split(" - Severity:")[0][len("### "):]
    assert len(title) <= 72
    assert not title.endswith(".")
    assert " - Severity: 🟢 Nit" in title_line

    first_sentence = render_review_report(
        title="T", target="doc.md", line_range=None,
        outcomes=[make_outcome("design", "completed")],
        findings=[make_finding("First sentence here. Second sentence follows.")])
    assert "### First sentence here - Severity: 🟡 Major" in first_sentence


def test_render_review_report_defaults_for_empty_fields() -> None:
    report = render_review_report(
        title="T", target="doc.md", line_range=None,
        outcomes=[make_outcome("design", "completed")],
        findings=[make_finding("Something is wrong.", scope="")])
    assert "* **Affected Scope:** unspecified" in report
    assert "* **Rationale & Recommendation:** None provided." in report
    assert "* **Proposed Edit:** None proposed" in report


# --------------------------------------------------------------------------- #
# render_consolidated
# --------------------------------------------------------------------------- #

def build_consolidated_fixture() -> list[StageReport]:
    return [
        make_stage(5, findings=(
            make_finding("Missing retry budget for the worker pool.", severity="blocker",
                         scope="L10-L20", finding_id="design-r5-1"),
            make_finding("Tighten the introduction.", severity="nit", reviewer="style",
                         scope="L1-L5", finding_id="style-r5-1"),
        ), outcomes=(make_outcome("design", "completed"),),
            title="Initial Zoom Out Review", path="zoom_out_report.md"),
        make_stage(7, findings=(
            make_finding("Tighten the introduction.", severity="major", reviewer="style",
                         scope="L1-L5", rationale="The opening buries the lede.",
                         finding_id="style-r7-1"),
            make_finding("Add a failure-mode table.", severity="major",
                         reviewer="zoom_out", scope="L30-L40",
                         finding_id="zoom_out-r7-1"),
        ), outcomes=(make_outcome("style", "completed"),
                    make_outcome("zoom_out", "failed")),
            title="Critic Review", path="critic_report.md"),
    ]


def test_render_consolidated_sections_and_matrix_integrity() -> None:
    stages = build_consolidated_fixture()
    report = render_consolidated(target="docs/design.md",
                                 split=Split(feature=LineRange(1, 40), detailed=LineRange(41, 80)),
                                 stages=stages)

    assert report.startswith("# Consolidated Review Report\n\n")
    assert "**Target:** `docs/design.md`" in report
    assert "**Semantic split:** feature `L1-L40`, detailed `L41-L80`" in report
    assert "## Severity Summary" in report
    assert "| 🔴 Blocker | 1 |" in report
    assert "| 🟡 Major | 2 |" in report
    assert "| 🟢 Nit | 0 |" in report
    assert "## Reviewers\n\n| Stage | Reviewer | Status |" in report
    assert "| 7 | `zoom_out` | not run |" in report
    assert "## Consolidated Findings" in report
    assert "#### BLOCKER-01 - Missing retry budget for the worker pool" in report
    assert "#### MAJOR-01 - Tighten the introduction" in report
    assert "#### MAJOR-02 - Add a failure-mode table" in report
    assert "also raised in stage 7" in report

    matrix = re.findall(
        r"^\| ((?:BLOCKER|MAJOR|NIT)-\d{2}) \| (.+?) \| (\d+) \| (Blocker|Major|Nit) \|$",
        report, re.MULTILINE)
    assert [row[0] for row in matrix] == ["BLOCKER-01", "MAJOR-01", "MAJOR-02"]
    assert matrix[1][1] == "style-r5-1, style-r7-1"
    assert matrix[1][2] == "5"
    assert matrix[1][3] == "Major"

    headings = re.findall(r"^#### ((?:BLOCKER|MAJOR|NIT)-\d{2}) - ", report, re.MULTILINE)
    assert headings == [row[0] for row in matrix]

    assert "## Stage Reports" in report
    assert "| 5 | `zoom_out_report.md` | 1 blocker, 0 major, 1 nit |" in report
    assert "| 7 | `critic_report.md` | 0 blocker, 2 major, 0 nit |" in report


def test_render_consolidated_handles_missing_split() -> None:
    report = render_consolidated(target="doc.md", split=None, stages=[])
    assert "**Semantic split:** feature `N/A`, detailed `N/A`" in report
    assert "## Consolidated Findings\n\nNo findings." in report


def test_render_consolidated_split_without_detailed() -> None:
    split = Split(feature=LineRange(1, 12), detailed=None)
    report = render_consolidated(target="doc.md", split=split, stages=[])
    assert "feature `L1-L12`, detailed `N/A`" in report


def test_render_consolidated_escapes_pipes_and_marks_skipped_stages() -> None:
    stages = [
        make_stage(9, findings=(), outcomes=(make_outcome("a|b", "completed"),),
                   skipped=True, path="impl.md"),
    ]
    report = render_consolidated(target="doc.md", split=None, stages=stages)
    assert "| 9 | `a\\|b` | completed |" in report
    assert "| 9 | `impl.md` | skipped |" in report


def test_render_consolidated_is_deterministic() -> None:
    stages = build_consolidated_fixture()
    first = render_consolidated(target="doc.md", split=None, stages=stages)
    second = render_consolidated(target="doc.md", split=None, stages=stages)
    assert first == second


# --------------------------------------------------------------------------- #
# merge_stage_findings
# --------------------------------------------------------------------------- #

def test_merge_stage_findings_dedupes_across_stages() -> None:
    stages = build_consolidated_fixture()
    merged = merge_stage_findings(stages)
    descriptions = [f.description for f in merged]
    assert descriptions.count("Tighten the introduction.") == 1
    assert len(merged) == 3

    tightened = next(f for f in merged if f.description == "Tighten the introduction.")
    assert tightened.severity == "major"
    assert tightened.id == "style-r5-1"  # earliest stage keeps the raw id
    assert tightened.rationale.endswith("also raised in stage 7")


def test_merge_stage_findings_keeps_distinct_reviewers_apart() -> None:
    text = "The retry budget is never defined for the worker pool."
    stages = [
        make_stage(5, findings=(make_finding(text, reviewer="design"),)),
        make_stage(7, findings=(make_finding(text, reviewer="style"),)),
    ]
    merged = merge_stage_findings(stages)
    assert len(merged) == 2
    assert {f.reviewer for f in merged} == {"design", "style"}


def test_merge_stage_findings_synthesizes_missing_ids() -> None:
    stages = [make_stage(6, findings=(make_finding("No id here."),))]
    merged = merge_stage_findings(stages)
    assert merged[0].id == "design-r6-1"


def test_parse_findings_accepts_an_empty_findings_array():
    assert parse_findings('{"reviewer": "design", "findings": []}', reviewer="design") == []
    assert parse_findings("[]", reviewer="style") == []
