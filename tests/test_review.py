from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from egm import review, tools
from egm.lint import lint_document, run_lint
from egm.types import DocumentError


DOC = """# Webhook Delivery

## Overview

Send events to subscribers. TBD: retry policy.

## Goals

- Deliver events.

## Non-Goals

- Not a message bus.

## Alternatives Considered

### Polling

Rejected by author: adds latency.

## Technical Plan

```text
producer -> queue -> worker
```

## Detailed Implementation Plan

### Files

- `src/webhooks.py` (Create)
"""


# ── fake host ─────────────────────────────────────────────────────────────

class FakeService:
    def __init__(self, *, empty_for=(), crash_for=()):
        self.requests: list[dict] = []
        self.empty_for = set(empty_for)
        self.crash_for = set(crash_for)

    def launch(self, request):
        payload = request if isinstance(request, dict) else request.__dict__
        self.requests.append(dict(payload))
        return {"id": len(self.requests)}

    def wait(self, handle, timeout_seconds=None):
        return types.SimpleNamespace(timed_out=False)

    def cancel(self, handle, reason):
        return None

    def result(self, handle):
        request = self.requests[handle["id"] - 1]
        reviewer = (request.get("metadata") or {}).get("reviewer", "design")
        if reviewer in self.crash_for:
            return types.SimpleNamespace(ready=True, terminal_state="FAILED",
                                         summary="", error_message="boom")
        if reviewer in self.empty_for:
            return types.SimpleNamespace(ready=True, terminal_state="SUCCEEDED", summary="")
        payload = {"reviewer": reviewer, "findings": [{
            "severity": "major", "scope": "L1-L2",
            "description": f"{reviewer} found a gap",
            "rationale": "because", "suggestion": "fix it"}]}
        return types.SimpleNamespace(ready=True, terminal_state="SUCCEEDED",
                                     summary=f"```json\n{json.dumps(payload)}\n```")


class FakeCtx:
    def __init__(self, service=None, settings=None):
        self.subagent_lifecycle = service
        self._settings = settings or {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected = content
        return True

    def dispatch_tool(self, name, args, **kwargs):
        return json.dumps({"results": [{"summary": json.dumps(
            {"reviewer": "design", "findings": []})}]})


def _handler(name):
    for registration in tools.iter_tool_registrations(FakeCtx()):
        if registration["name"] == name:
            return registration["handler"]
    raise KeyError(name)


# ── lint ──────────────────────────────────────────────────────────────────

def test_lint_flags_placeholder_and_missing_sections(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n## Overview\n\nTBD\n", encoding="utf-8")
    result = run_lint(path, "feature-spec")
    descriptions = " ".join(f["description"] for f in result["findings"])
    assert "Placeholder" in descriptions
    assert "Required section missing" in descriptions
    assert result["counts"]["major"] >= 1


def test_lint_unclosed_fence_is_blocker(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n## Overview\n\n```text\nunclosed\n", encoding="utf-8")
    result = run_lint(path)
    assert any(f["severity"] == "blocker" and "Unclosed" in f["description"]
               for f in result["findings"])


def test_lint_heading_jump_and_empty_section(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n### Deep\n\n## Empty\n\n## Next\n\ntext\n", encoding="utf-8")
    findings = lint_document(__import__("egm.documents", fromlist=["read_document"]).read_document(path))
    text = " ".join(f.description for f in findings)
    assert "Heading level jumps" in text


def test_lint_alternatives_rule_is_blocker(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n## Overview\n\n## Problem\n\n## Goals\n\n## Non-Goals\n\n"
                    "## Technical Plan\n\n## Risks\n\nNone.\n", encoding="utf-8")
    result = run_lint(path, "feature-spec")
    assert any(f["severity"] == "blocker" and "Alternatives" in f["description"]
               for f in result["findings"])


def test_lint_broken_link(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n## Overview\n\nSee [missing](./nope.md).\n", encoding="utf-8")
    result = run_lint(path)
    assert any("does not exist" in f["description"] for f in result["findings"])


# ── tools ─────────────────────────────────────────────────────────────────

def test_lint_tool_returns_json(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    payload = json.loads(_handler("egm_lint")({"target": str(path)}))
    assert payload["ok"] is True
    assert "counts" in payload


def test_bootstrap_plan_tool(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    payload = json.loads(_handler("egm_bootstrap_plan")({"path": str(tmp_path)}))
    assert payload["ok"] is True
    assert payload["plan"]["directory_count"] >= 1


def test_status_tool_without_state(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    payload = json.loads(_handler("egm_status")({"target": str(path)}))
    assert payload["ok"] is True
    assert payload["rounds"] == []


def test_tool_error_is_returned_not_raised(tmp_path: Path):
    payload = json.loads(_handler("egm_lint")({"target": str(tmp_path / "missing.md")}))
    assert payload["ok"] is False
    assert "does not exist" in payload["error"]


# ── review orchestration ──────────────────────────────────────────────────

def test_full_review_writes_all_reports(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    service = FakeService()
    ctx = FakeCtx(service)
    result = review.run_review(ctx, path, review.ReviewOptions(preset="full"),
                               {"retries": 0, "max_context_chars": 26000})
    assert len(result.stages) == 5
    names = {Path(s.path).name for s in result.stages if s.path}
    assert {"zoom_out_report.md", "comprehension_report.md", "critic_report.md",
            "intermediate_zoom_out_report.md", "implementation_readiness_report.md"} <= names
    assert Path(result.consolidated_path).exists()
    assert all(o.status == "completed" for s in result.stages for o in s.outcomes)
    assert sum(result.as_dict()["counts"].values()) > 0
    # Stage 6 must run one reviewer only and must not receive a reading list.
    stage6 = next(s for s in result.stages if s.stage == 6)
    assert [o.spec.name for o in stage6.outcomes] == ["comprehension"]


def test_review_context_is_isolated_from_conversation(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    service = FakeService()
    review.run_review(FakeCtx(service), path, review.ReviewOptions(stage="7"),
                      {"retries": 0})
    assert len(service.requests) == 3
    forbidden = {"messages", "history", "transcript", "conversation", "system_prompt"}
    for request in service.requests:
        assert not (set(request) & forbidden)
        assert json.dumps(request.get("metadata", {})) in (
            '{"plugin": "egm", "reviewer": "design"}',
            '{"plugin": "egm", "reviewer": "structural"}',
            '{"plugin": "egm", "reviewer": "style"}')


def test_failed_reviewer_is_not_run_not_zero_findings(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    service = FakeService(crash_for={"structural"})
    result = review.run_review(FakeCtx(service), path, review.ReviewOptions(stage="7"),
                               {"retries": 0})
    outcome = next(o for o in result.stages[0].outcomes if o.spec.name == "structural")
    assert outcome.status == "not run"
    report = Path(result.stages[0].path).read_text(encoding="utf-8")
    assert "not run" in report


def test_implementation_preset_skips_without_plan(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\n## Overview\n\nNo plan here.\n", encoding="utf-8")
    result = review.run_review(FakeCtx(FakeService()), path,
                               review.ReviewOptions(preset="implementation"), {"retries": 0})
    assert result.stages[0].skipped is True
    assert "No implementation plan" in result.stages[0].note


def test_feature_spec_preset_stops_before_readiness(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    result = review.run_review(FakeCtx(FakeService()), path,
                               review.ReviewOptions(preset="feature_spec"), {"retries": 0})
    assert [s.stage for s in result.stages] == [5, 6, 7, 8]


def test_loop_round_persists_and_verdicts(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    payload = review.run_loop_round(
        FakeCtx(FakeService()), path, reviewers=["design", "style"],
        settings={"retries": 0}, state_base=tmp_path / "state")
    assert payload["ok"] is True
    assert payload["round"] == 1
    assert payload["verdict"]["converged"] is False
    assert Path(payload["report_path"]).exists()
    assert Path(payload["state_path"]).exists()


def test_brainstorm_writes_full_brief(tmp_path: Path):
    payload = review.run_brainstorm(
        FakeCtx(FakeService()), "Add webhook delivery with retries.",
        reviewers=["design", "style"], out="concepts_brief.md",
        author="Ada", settings={"retries": 0}, root=str(tmp_path))
    text = Path(payload["path"]).read_text(encoding="utf-8")
    for heading in ("## Overview", "## Analysis", "## Goals vs. Non-Goals",
                    "## Alternatives Weighed", "## Key Decisions & Staging for Next Steps"):
        assert heading in text
    assert "Ada" in text
    assert payload["counts"]["major"] >= 1


def test_resolve_semantic_target_is_used(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    payload = review.run_loop_round(FakeCtx(FakeService()), path,
                                    reviewers=["style"], semantic_target="## Goal",
                                    settings={"retries": 0}, state_base=tmp_path / "state")
    assert payload["range"]["start"] > 1


class NoParentService(FakeService):
    def launch(self, request):
        raise RuntimeError("No active Hermes parent session is available.")


def test_lifecycle_no_parent_falls_back_to_delegate(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    ctx = FakeCtx(NoParentService())
    result = review.run_review(ctx, path, review.ReviewOptions(stage="7"), {"retries": 0})
    assert len(result.stages[0].outcomes) == 3
    assert all(o.status == "completed" for o in result.stages[0].outcomes)


class ProseService(FakeService):
    def result(self, handle):
        request = self.requests[handle["id"] - 1]
        reviewer = (request.get("metadata") or {}).get("reviewer", "comprehension")
        payload = json.dumps({"reviewer": reviewer, "findings": [{
            "severity": "major", "scope": "L2", "description": "Undefined term",
            "rationale": "A fresh reader cannot act on it."}]})
        prose = ("## What this proposes\n\nDeliver events.\n\n"
                 "## How the existing system works, per this document\n\nIt does not say.")
        return types.SimpleNamespace(ready=True, terminal_state="SUCCEEDED",
                                     summary=f"{prose}\n\n---\n\n```json\n{payload}\n```")


def test_comprehension_report_keeps_prose_and_drops_the_json(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    result = review.run_review(FakeCtx(ProseService()), path,
                               review.ReviewOptions(stage="6"), {"retries": 0})
    text = Path(result.stages[0].path).read_text(encoding="utf-8")
    assert "## What this proposes" in text
    assert "## Findings" in text
    assert "```json" not in text
    assert "Undefined term" in text


def test_single_stage_run_writes_no_consolidated_report(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    result = review.run_review(FakeCtx(FakeService()), path,
                               review.ReviewOptions(stage="7"), {"retries": 0})
    assert result.consolidated_path == ""
    assert not (tmp_path / "consolidated_review_report.md").exists()
    assert result.preset == "stage:7"


class UniqueCorrelationService(FakeService):
    """Mimics the host: a repeated correlation_id for one parent is an error."""

    def launch(self, request):
        payload = request if isinstance(request, dict) else request.__dict__
        correlation = payload.get("correlation_id")
        if correlation in getattr(self, "_seen", set()):
            raise RuntimeError("Duplicate correlation_id for this parent session.")
        self._seen = getattr(self, "_seen", set()) | {correlation}
        return super().launch(request)


def test_full_review_never_reuses_a_correlation_id(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    result = review.run_review(FakeCtx(UniqueCorrelationService()), path,
                               review.ReviewOptions(preset="full"), {"retries": 0})
    assert all(o.status == "completed" for s in result.stages for o in s.outcomes)


def test_loop_round_honours_report_name(tmp_path: Path):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    payload = review.run_loop_round(
        FakeCtx(FakeService()), path, reviewers=["style"],
        settings={"retries": 0}, state_base=tmp_path / "state",
        report_name="critic_report.md")
    assert payload["report_path"].endswith("critic_report.md")
    assert Path(payload["report_path"]).exists()


def test_drift_documents_skips_generated_reports(tmp_path: Path):
    (tmp_path / "design.md").write_text("# d\n", encoding="utf-8")
    (tmp_path / "critic_report.md").write_text("# r\n", encoding="utf-8")
    (tmp_path / "2026-01-01_DRIFT_REPORT.md").write_text("# r\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.md").write_text("# x\n", encoding="utf-8")
    names = [p.name for p in review.drift_documents(tmp_path)]
    assert names == ["design.md"]
