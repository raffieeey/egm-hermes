from __future__ import annotations

import json
from pathlib import Path

import pytest

from egm import catalog
from egm.state import ReviewState, fingerprint, state_path
from egm.types import Finding, ReviewerOutcome, ReviewerSpec, LineRange, Split, StageReport


def _finding(desc="Missing retry bound", reviewer="design", severity="major", scope="L10-L20"):
    return Finding(reviewer=reviewer, severity=severity, scope=scope, description=desc)


def _outcome(name="design", status="completed"):
    return ReviewerOutcome(spec=catalog.reviewer(name), status=status,
                           findings=(), raw="report")


def test_fingerprint_ignores_severity_and_line_numbers():
    a = _finding(severity="major", scope="L10-L20")
    b = _finding(severity="nit", scope="L99-L120")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(_finding(desc="Different issue"))


def test_settle_prevents_reopening(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    finding = _finding()
    assert state.settle([finding], "contested") == 1
    assert state.settle([finding], "contested") == 0
    record = state.record([finding], [_outcome()])
    assert record.clean is True  # the only finding is settled


def test_not_run_reviewer_makes_round_unclean(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    record = state.record([], [_outcome("design"), _outcome("style", "not run")])
    assert record.clean is False
    assert record.not_run == ["style"]


def test_blocker_keeps_round_open(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    record = state.record([_finding(severity="blocker")], [_outcome()])
    assert record.clean is False
    assert state.verdict().converged is False


def test_convergence_requires_two_clean_rounds(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    state.record([], [_outcome()])
    assert state.verdict().converged is False
    assert "one more" in state.verdict().reason
    state.record([], [_outcome()])
    verdict = state.verdict()
    assert verdict.converged is True
    assert verdict.clean is True


def test_cap_reached(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    for _ in range(5):
        state.record([_finding(desc="open issue", severity="blocker")], [_outcome()])
    verdict = state.verdict(max_rounds=5)
    assert verdict.cap_reached is True
    assert verdict.converged is False


def test_churn_detected_at_fixed_scope(tmp_path: Path):
    state = ReviewState(tmp_path / "s.json")
    state.mark_fixed(["L10-L20"])
    record = state.record([_finding(severity="major", scope="L10-L20")], [_outcome()])
    assert record.new_at_fixed_scope == ["L10-L20"]
    assert state.verdict().churn is True


def test_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "s.json"
    state = ReviewState(path)
    state.record([_finding()], [_outcome()])
    state.settle([_finding()], "accepted risk")
    state.save()
    reloaded = ReviewState(path)
    assert reloaded.round_count == 1
    assert len(reloaded.settled) == 1
    assert reloaded.rounds[0].counts["major"] == 1


def test_state_path_is_stable_and_project_scoped(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    doc = root / "docs" / "design.md"
    doc.parent.mkdir()
    doc.write_text("# x\n", encoding="utf-8")
    a = state_path(root, doc, base=tmp_path / "base")
    b = state_path(root, doc, base=tmp_path / "base")
    assert a == b
    other = tmp_path / "other"
    other.mkdir()
    assert state_path(other, doc, base=tmp_path / "base") != a


def test_catalog_every_reviewer_resolves_and_assets_exist():
    for name in catalog.REVIEWER_SHORTNAMES:
        spec = catalog.reviewer(name)
        assert spec.persona
        assert catalog.persona_text(name).strip()
        if spec.standard:
            assert catalog.standard_text(spec.standard).strip()
    assert catalog.persona_text("egm-design").strip()
    assert catalog.standard_text("feature-spec-standard").strip()


def test_catalog_aliases_and_errors():
    assert catalog.canonical_reviewer("design_critic") == "design"
    assert catalog.canonical_reviewer("egm-zoom-out") == "zoom_out"
    with pytest.raises(Exception):
        catalog.canonical_reviewer("nope")
    with pytest.raises(Exception):
        catalog.stage(11)


def test_presets_expand_in_order():
    assert [s.number for s in catalog.preset_stages("full")] == [5, 6, 7, 8, 9]
    assert [s.number for s in catalog.preset_stages("feature_spec")] == [5, 6, 7, 8]
    assert [s.number for s in catalog.preset_stages("implementation")] == [9]
    assert catalog.preset_stages("7")[0].key == "critic"


def test_stage_contract_matches_upstream():
    stage5 = catalog.stage(5)
    assert stage5.reviewers == ("zoom_out", "inventive_catalyst")
    assert stage5.report_name == "zoom_out_report.md"
    assert catalog.stage(6).uses_context is False
    assert catalog.stage(7).reviewers == ("design", "structural", "style")
    assert catalog.stage(8).report_name == "intermediate_zoom_out_report.md"
    assert catalog.stage(9).reviewers == (
        "implementation_readiness", "codebase_details", "style")


def test_inventories_are_json_serialisable():
    payload = catalog.inventories()
    json.dumps(payload)
    assert len(payload["reviewers"]) == 8
    assert len(payload["stages"]) == 5
    assert len(payload["standards"]) >= 5


def test_review_lock_is_exclusive(tmp_path: Path):
    from egm.reports import review_lock
    target = tmp_path / "doc.md"
    target.write_text("# x\n", encoding="utf-8")
    with review_lock(target):
        with pytest.raises(Exception):
            with review_lock(target):
                pass
    with review_lock(target):
        pass


def test_review_lock_takes_over_a_stale_lock(tmp_path: Path):
    import os
    import time
    from egm.reports import review_lock
    target = tmp_path / "doc.md"
    target.write_text("# x\n", encoding="utf-8")
    with review_lock(target) as path:
        os.utime(path, (time.time() - 7200, time.time() - 7200))
    with review_lock(target, stale_seconds=60):
        pass


def test_settle_refs_uses_recorded_finding_ids(tmp_path: Path):
    from egm.synth import normalize
    from egm.types import EGMError
    state = ReviewState(tmp_path / "s.json")
    finding = normalize([_finding()], reviewer="design", round_no=1)[0]
    state.record([finding], [_outcome()])
    assert state.settle_refs([finding.id]) == 1
    assert state.settle_refs([finding.id]) == 0
    with pytest.raises(EGMError):
        state.settle_refs(["design-r9-99"])
