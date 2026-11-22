"""Review orchestration: presets, the five stages, brainstorm, loop, drift.

The skills decide *when* to run a stage; this module decides *what* a stage is —
which reviewers, which line range, which rubric, which artifact — and then hands
the work to the swarm. Keeping that mapping in code means the five-stage
pipeline cannot drift between the skill text, the tool schema and the CLI.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import catalog, documents, prompts, reports, synth
from .drift import DriftScan, report_filename, render_report, repo_searcher, scan_document
from .state import ReviewState, Verdict, state_path
from .swarm import ReviewJob, SwarmRunner, build_swarm
from .types import (EGMError, Document, DocumentError, Finding, LineRange,
                    ReviewReport, ReviewerOutcome, ReviewerSpec, Split, StageReport,
                    SEVERITY_LABEL, STATUS_COMPLETED, severity_counts, sort_findings)

DEFAULTS: dict[str, Any] = {
    "max_reviewers": 6,
    "reviewer_model": "",
    "reviewer_provider": "",
    "retries": 1,
    "max_context_chars": 26_000,
    "review_timeout_seconds": 600,
    "dual_run": False,
    "report_dir": "",
}


# ── settings ──────────────────────────────────────────────────────────────

def read_settings(ctx: Any | None) -> dict[str, Any]:
    """Read ``plugins.entries.egm.settings`` with defaults, tolerating any host."""
    settings = dict(DEFAULTS)
    if ctx is None:
        return settings
    getter = getattr(ctx, "get_config", None)
    if getter is None:
        return settings
    for key, default in DEFAULTS.items():
        try:
            value = getter(key, default)
        except Exception:  # noqa: BLE001 - a bad config key must not break a review
            continue
        if value is not None and value != "":
            settings[key] = value
        elif value == "" and isinstance(default, str):
            settings[key] = ""
    return settings


@dataclass(frozen=True)
class ReviewOptions:
    """One invocation's parameters."""

    preset: str = "full"
    stage: str = ""
    reviewers: tuple[str, ...] = ()
    standards: dict[str, str] = field(default_factory=dict)
    context: tuple[str, ...] = ()
    write_reports: bool = True
    report_dir: str = ""

    def stages(self) -> tuple[catalog.Stage, ...]:
        if self.stage:
            return (catalog.stage(self.stage),)
        return catalog.preset_stages(self.preset)


# ── planning ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StagePlan:
    stage: catalog.Stage
    line_range: LineRange | None
    reviewers: tuple[ReviewerSpec, ...]
    skipped: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.number, "key": self.stage.key, "title": self.stage.title,
                "range": self.line_range.as_dict() if self.line_range else None,
                "report": self.stage.report_name, "skipped": self.skipped, "note": self.note,
                "reviewers": [r.as_dict() for r in self.reviewers]}


def _split_for(target: Path, preset: str) -> Split:
    doc = documents.read_document(target)
    if preset.lower().replace("-", "_") in {"feature_spec", "featurespec", "feature"}:
        return Split(feature=doc.full_range, detailed=None, boundary_line=None,
                     boundary_title="feature spec only")
    return documents.discover_split(doc)


def _range_for(stage: catalog.Stage, doc: Document, split: Split) -> tuple[LineRange | None, str]:
    if stage.range_key == "full":
        return doc.full_range, ""
    if stage.range_key == "feature":
        return split.feature, ""
    if stage.range_key == "both":
        return doc.full_range, ""
    if stage.range_key == "detailed":
        if split.detailed is None:
            return None, ("No implementation plan was detected, so there is nothing for the "
                          "readiness stage to audit. Add a '## Detailed Implementation Plan' "
                          "section or run the feature-spec preset.")
        return split.detailed, ""
    raise EGMError(f"Stage {stage.number} has an unknown range key {stage.range_key!r}.")


def plan_review(target: str | Path, options: ReviewOptions) -> tuple[Document, Split, list[StagePlan]]:
    """Resolve the document, its split and exactly which reviewers each stage uses."""
    path = reports.resolve_target(target)
    if path.is_dir():
        raise DocumentError(f"Target must be a document, not a directory: {path}")
    doc = documents.read_document(path)
    split = _split_for(path, options.preset if not options.stage else "full")
    overrides = tuple(catalog.canonical_reviewer(name) for name in options.reviewers)
    plans: list[StagePlan] = []
    for stage in options.stages():
        line_range, note = _range_for(stage, doc, split)
        names = overrides or stage.reviewers
        specs = tuple(_spec_with_standard(name, options.standards) for name in names)
        plans.append(StagePlan(stage, line_range, specs, skipped=line_range is None, note=note))
    return doc, split, plans


def _align_outcomes(outcomes: Sequence[ReviewerOutcome],
                   findings: Sequence[Finding]) -> list[ReviewerOutcome]:
    """Make each reviewer's findings the same objects as the merged list.

    Normalization assigns ids to the merged list, while an outcome still holds
    the ids parsing gave it. Two numbering schemes for one finding make
    "settle id X" ambiguous, so the nested view is rebuilt from the final list.
    """
    by_reviewer: dict[str, list[Finding]] = {}
    for finding in findings:
        by_reviewer.setdefault(finding.reviewer, []).append(finding)
    return [dataclasses.replace(o, findings=tuple(by_reviewer.get(o.spec.name, ())))
            for o in outcomes]


def _spec_with_standard(name: str, overrides: dict[str, str]) -> ReviewerSpec:
    canonical = catalog.canonical_reviewer(name)
    standard = None
    for key, value in (overrides or {}).items():
        if catalog.canonical_reviewer(key) == canonical:
            standard = value
            break
    return catalog.reviewer(canonical, standard)


# ── context assembly ──────────────────────────────────────────────────────

def _linked_context(doc: Document, root: Path) -> list[str]:
    """Auto-include links that stay inside the working directory (upstream Step 1.3)."""
    try:
        linked = documents.linked_files(doc, root)
    except Exception:  # noqa: BLE001 - a malformed link must not stop a review
        return []
    return [str(p) for p in linked.included]


def _context_paths(doc: Document, options: ReviewOptions, stage: catalog.Stage,
                   report_dir: Path) -> list[str]:
    paths: list[str] = []
    if stage.uses_context:
        paths.extend(_linked_context(doc, doc.path.parent))
        paths.extend(options.context)
        if stage.adds_report_to_context:
            candidate = report_dir / stage.adds_report_to_context
            if candidate.exists():
                paths.insert(0, str(candidate))
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = str(Path(path).expanduser().resolve()) if Path(path).expanduser().exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


# ── stage execution ───────────────────────────────────────────────────────

def _jobs_for_plan(plan: StagePlan, doc: Document, context_paths: list[str],
                   settings: dict[str, Any]) -> list[ReviewJob]:
    assert plan.line_range is not None
    budget = int(settings.get("max_context_chars") or 26_000)
    files = prompts.read_context_files(context_paths, root=doc.path.parent,
                                       budget=max(2_000, budget // 2))
    jobs: list[ReviewJob] = []
    for spec in plan.reviewers:
        persona = catalog.persona_text(spec.name)
        standard = catalog.standard_text(spec.standard) if spec.standard else ""
        bundles = prompts.chunk_plan(
            doc, plan.line_range, spec=spec, persona=persona, standard_text=standard,
            context_files=files, mode="review", extra=plan.note, budget=budget,
        )
        jobs.append(ReviewJob(spec, tuple(bundles)))
    return jobs


def run_stage(ctx: Any, doc: Document, split: Split, plan: StagePlan, options: ReviewOptions,
              settings: dict[str, Any], out_dir: Path,
              runner: SwarmRunner | None = None) -> StageReport:
    """Run one stage, write its artifact, and return its findings."""
    if plan.skipped or plan.line_range is None:
        return StageReport(plan.stage.number, plan.stage.key, plan.stage.title, "",
                           skipped=True, note=plan.note)
    context_paths = _context_paths(doc, options, plan.stage, out_dir)
    jobs = _jobs_for_plan(plan, doc, context_paths, settings)
    runner = runner or build_swarm(ctx, settings)
    outcomes = runner.run(jobs)

    raw_findings: list[Finding] = []
    for outcome in outcomes:
        raw_findings.extend(synth.normalize(outcome.findings, reviewer=outcome.spec.name, round_no=1))
    findings = synth.dedupe(raw_findings)
    outcomes = _align_outcomes(outcomes, findings)

    body = "\n\n".join(o.raw.strip() for o in outcomes if o.raw.strip())
    if plan.stage.key == "comprehension":
        # The paraphrase is the deliverable; the JSON block was only for counting.
        prose = "\n\n".join(part for part in
                             (synth.strip_finding_payload(o.raw) for o in outcomes) if part)
        lines = [f"**Target:** `{doc.path}` (`{plan.line_range.label}`)", ""]
        if prose:
            lines.extend([prose, ""])
        lines.extend(["## Findings", ""])
        if findings:
            for finding in findings:
                detail = (f"- **{SEVERITY_LABEL[finding.severity]}** `{finding.scope}` — "
                          f"{finding.description}")
                if finding.rationale:
                    detail += f" _{finding.rationale}_"
                lines.append(detail)
        else:
            lines.append("No findings.")
        content = "\n".join(lines)
    else:
        content = synth.render_review_report(
            title=plan.stage.title, target=str(doc.path), line_range=plan.line_range,
            outcomes=outcomes, findings=findings, mode="review",
        )
    path = ""
    if options.write_reports:
        path = str(reports.write_report(out_dir, plan.stage.report_name, content))
    return StageReport(plan.stage.number, plan.stage.key, plan.stage.title, path,
                       outcomes=tuple(outcomes), findings=tuple(findings),
                       body=content, note=plan.note)


def run_review(ctx: Any, target: str | Path, options: ReviewOptions | None = None,
               settings: dict[str, Any] | None = None) -> ReviewReport:
    """Run a preset or a single stage end to end."""
    options = options or ReviewOptions()
    settings = settings or read_settings(ctx)
    doc, split, plans = plan_review(target, options)
    out_dir = reports.report_directory(doc.path, str(settings.get("report_dir") or options.report_dir))
    runner = build_swarm(ctx, settings)
    label = f"stage:{options.stage}" if options.stage else (options.preset or "full")
    result = ReviewReport(target=str(doc.path), preset=label, split=split)
    with reports.review_lock(doc.path):
        for plan in plans:
            result.stages.append(run_stage(ctx, doc, split, plan, options, settings, out_dir, runner))
        result.findings = tuple(synth.merge_stage_findings(result.stages))
        if options.write_reports and len(plans) > 1:
            consolidated = synth.render_consolidated(target=str(doc.path), split=split,
                                                     stages=result.stages)
            result.consolidated_path = str(reports.write_report(
                out_dir, "consolidated_review_report.md", consolidated))
    return result


# ── iterative loop ────────────────────────────────────────────────────────

def run_loop_round(ctx: Any, target: str | Path, *, reviewers: Sequence[str],
                   standards: dict[str, str] | None = None, semantic_target: str = "full file",
                   context: Sequence[str] = (), settings: dict[str, Any] | None = None,
                   max_rounds: int = 5, state_base: Path | None = None,
                   report_name: str = "goldfish_review_report.md",
                   settled: Sequence[str] = (),
                   fixed_scopes: Sequence[str] = ()) -> dict[str, Any]:
    """Run exactly one loop round, persist it, and return the findings plus verdict."""
    settings = settings or read_settings(ctx)
    if not reviewers:
        raise EGMError("egm_review_loop requires at least one reviewer.")
    path = reports.resolve_target(target)
    doc = documents.read_document(path)
    line_range = doc.resolve(semantic_target)
    specs = tuple(catalog.reviewer(name) for name in reviewers)
    files = prompts.read_context_files(list(context), root=doc.path.parent,
                                       budget=max(2_000, int(settings.get("max_context_chars") or 26_000) // 2))
    budget = int(settings.get("max_context_chars") or 26_000)
    jobs = []
    for spec in specs:
        persona = catalog.persona_text(spec.name)
        standard = catalog.standard_text(spec.standard) if spec.standard else ""
        bundles = prompts.chunk_plan(doc, line_range, spec=spec, persona=persona,
                                     standard_text=standard, context_files=files,
                                     mode="review", extra="", budget=budget)
        jobs.append(ReviewJob(spec, tuple(bundles)))
    with reports.review_lock(doc.path):
        state = ReviewState(state_path(doc.path.parent, doc.path, base=state_base))
        state.target = str(doc.path)
        round_no = state.round_count + 1
        outcomes = (build_swarm(ctx, settings)).run(jobs)
        findings = synth.dedupe([
            f for o in outcomes
            for f in synth.normalize(o.findings, reviewer=o.spec.name, round_no=round_no)])
        outcomes = _align_outcomes(outcomes, findings)

        if fixed_scopes:
            state.mark_fixed(list(fixed_scopes))
        settled_applied = state.settle_refs(list(settled)) if settled else 0
        record = state.record(findings, outcomes)
        verdict: Verdict = state.verdict(max_rounds=max_rounds)

        out_dir = reports.report_directory(doc.path, str(settings.get("report_dir") or ""))
        report = synth.render_review_report(
            title=f"Goldfish Review Report — Round {record.round}", target=str(doc.path),
            line_range=line_range, outcomes=outcomes, findings=findings, mode="review",
        )
        report_path = str(reports.write_report(out_dir, report_name or "goldfish_review_report.md", report))
    return {
        "ok": True,
        "target": str(doc.path),
        "range": line_range.as_dict(),
        "round": record.round,
        "counts": severity_counts(findings),
        "findings": [f.as_dict() for f in sort_findings(findings)],
        "reviewers": [o.as_dict() for o in outcomes],
        "verdict": verdict.as_dict(),
        "settled_applied": settled_applied,
        "fixed_scopes": list(fixed_scopes),
        "report_path": report_path,
        "state_path": str(state.path),
    }


# ── brainstorm ────────────────────────────────────────────────────────────

_BRIEF_TEMPLATE = """# {title}

* **Author:** {author}
* **Date:** {date}

## Overview

{overview}

## Analysis

{analysis}

## Goals vs. Non-Goals

_Pending: the author and the Elephant record in-scope goals and explicit non-goals here._

## Alternatives Weighed

_Pending: record each rejected option and its technical reason here. `/egm-draft` copies this section into Alternatives Considered._

## Key Decisions & Staging for Next Steps

_Pending: record the decisions that survived review, and what `/egm-draft` must carry forward._
"""


def run_brainstorm(ctx: Any, hypothesis: str, *, reviewers: Sequence[str] = (),
                   context: Sequence[str] = (), out: str = "concepts_brief.md",
                   author: str = "", settings: dict[str, Any] | None = None,
                   root: str | Path | None = None) -> dict[str, Any]:
    """Run the seven-lens brainstorm swarm and write ``concepts_brief.md``."""
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise EGMError("egm_brainstorm requires a feature hypothesis.")
    settings = settings or read_settings(ctx)
    names = tuple(catalog.canonical_reviewer(n) for n in (reviewers or catalog.BRAINSTORM_LENSES))
    base = Path(root).expanduser().resolve() if root else Path.cwd()
    budget = int(settings.get("max_context_chars") or 26_000)
    files = prompts.read_context_files(list(context), root=base, budget=max(2_000, budget // 2))
    jobs = []
    for name in names:
        spec = catalog.reviewer(name)
        bundle = prompts.compose_brainstorm(spec=spec, persona=catalog.persona_text(name),
                                            hypothesis=hypothesis, context_files=files,
                                            author=author)
        jobs.append(ReviewJob(spec, (bundle,)))
    outcomes = build_swarm(ctx, settings).run(jobs)
    findings = synth.dedupe([f for o in outcomes
                             for f in synth.normalize(o.findings, reviewer=o.spec.name, round_no=1)])

    rows = ["| Lens | Status | Blockers | Majors | Nits |", "| --- | --- | --- | --- | --- |"]
    for outcome in outcomes:
        counts = severity_counts(outcome.findings)
        rows.append(f"| `{outcome.spec.name}` | {outcome.status} | {counts['blocker']} | "
                    f"{counts['major']} | {counts['nit']} |")
    analysis = ["### Reviewer status", "", *rows, ""]
    if not findings:
        analysis.extend(["No lens raised a material objection. The concept survived this review.", ""])
    for severity in ("blocker", "major", "nit"):
        group = [f for f in sort_findings(findings) if f.severity == severity]
        if not group:
            continue
        analysis.append(f"### {severity.title()} findings")
        analysis.append("")
        for finding in group:
            detail = f"- **`{finding.reviewer}` · {finding.scope or 'concept'}** — {finding.description}"
            if finding.rationale:
                detail += f" _{finding.rationale}_"
            analysis.append(detail)
        analysis.append("")

    title = hypothesis.splitlines()[0].lstrip("# ").strip()[:80] or "Concept Brief"
    content = _BRIEF_TEMPLATE.format(
        title=title, author=author or "(unspecified)", date=_dt.date.today().isoformat(),
        overview=hypothesis, analysis="\n".join(analysis),
    )
    directory = reports.report_directory(base / "concepts_brief.md", "")
    path = reports.write_report(directory, Path(out).name or "concepts_brief.md", content)
    return {"ok": True, "path": str(path), "counts": severity_counts(findings),
            "findings": [f.as_dict() for f in sort_findings(findings)],
            "reviewers": [o.as_dict() for o in outcomes]}


# ── drift ─────────────────────────────────────────────────────────────────

_GENERATED_REPORT_RE = re.compile(r"(?:_report|_DRIFT_REPORT)\.md$", re.IGNORECASE)


def drift_documents(target: Path) -> list[Path]:
    """Documents worth checking for drift: no dot paths, no generated reports."""
    if target.is_file():
        return [target]
    docs = sorted(
        p for p in target.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(target).parts)
        and not _GENERATED_REPORT_RE.search(p.name)
    )
    return docs


_DRIFT_SPEC = "codebase_details"


def run_drift(ctx: Any, target: str | Path, *, context: Sequence[str] = (),
              verify: bool = True, settings: dict[str, Any] | None = None,
              date: str = "") -> dict[str, Any]:
    """Compare documents with the code: deterministic mentions first, reviewers second."""
    settings = settings or read_settings(ctx)
    path = reports.resolve_target(target)
    root = path if path.is_dir() else path.parent
    docs = drift_documents(path)
    if not docs:
        raise DocumentError(f"No markdown documents found under {path}")
    scans: list[DriftScan] = []
    parsed: list[Document] = []
    search = repo_searcher(root)
    for doc_path in docs:
        try:
            document = documents.read_document(doc_path)
        except DocumentError:
            continue
        parsed.append(document)
        scans.append(scan_document(document, root, search=search))

    semantic: list[tuple[str, list[Finding], list[ReviewerOutcome]]] = []
    if verify and parsed:
        spec = catalog.reviewer(_DRIFT_SPEC)
        persona = catalog.persona_text(spec.name)
        standard = catalog.standard_text(spec.standard) if spec.standard else ""
        budget = int(settings.get("max_context_chars") or 26_000)
        files = prompts.read_context_files(list(context), root=root, budget=max(2_000, budget // 2))
        jobs = []
        for document in parsed:
            extra = ("Drift audit. Compare what this document claims exists now with the current "
                     "source tree. Report only divergences: missing features, incomplete models, "
                     "logic shortcuts, architectural divergence, and claims the code contradicts.")
            bundles = prompts.chunk_plan(document, document.full_range, spec=spec, persona=persona,
                                         standard_text=standard, context_files=files,
                                         mode="review", extra=extra, budget=budget)
            jobs.append((document, ReviewJob(spec, tuple(bundles))))
        runner = build_swarm(ctx, settings)
        for document, job in jobs:
            outcomes = runner.run([job])
            findings = [f for o in outcomes
                        for f in synth.normalize(o.findings, reviewer=o.spec.name, round_no=1)]
            semantic.append((str(document.path), findings, outcomes))

    today = date or _dt.date.today().isoformat()
    body = render_report(scans, date=today)
    if semantic:
        body += "\n\n## Semantic findings (fresh-context reviewers)\n"
        for doc_path, findings, outcomes in semantic:
            body += f"\n### `{doc_path}`\n\n"
            if not findings:
                body += "- No semantic drift detected.\n"
                continue
            for finding in sort_findings(findings):
                body += (f"- {finding.severity.title()}: {finding.description}"
                         f" ({finding.scope or 'no scope'})\n")
            failed = [o.spec.name for o in outcomes if o.status != STATUS_COMPLETED]
            if failed:
                body += f"- Reviewers not run: {', '.join(failed)}\n"
    out_dir = reports.report_directory(root / "drift.md", "")
    report_path = reports.write_report(out_dir, report_filename(today), body)
    return {"ok": True, "report_path": str(report_path), "date": today,
            "documents": len(scans),
            "deterministic_missing": sum(len(s.missing) for s in scans),
            "semantic_findings": sum(len(f) for _, f, _ in semantic)}


def quick_status(target: str | Path, *, state_base: Path | None = None) -> dict[str, Any]:
    """Loop state for a target, or a clear "nothing recorded" answer."""
    path = reports.resolve_target(target)
    state = ReviewState(state_path(path.parent, path, base=state_base))
    data = state.as_dict()
    data["ok"] = True
    return data
