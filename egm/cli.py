"""``hermes egm`` — inspection and deterministic operations from the terminal.

Anything that needs fresh-context children runs in a chat, not here: this CLI is
for the parts that must be true without a model — inventories, the semantic-split
plan, lint, drift without semantic review, bootstrap planning, and loop state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import catalog, documents, review
from .bootstrap import build_plan
from .drift import render_report, report_filename, scan_document
from .lint import run_lint
from .reports import resolve_target
from .review import drift_documents
from .types import EGMError, severity_counts

SKILLS: tuple[tuple[str, str], ...] = (
    ("egm-brainstorm", "Turn a half-formed idea into a reviewed concept brief."),
    ("egm-draft", "Draft a design doc section by section, with approval gates."),
    ("egm-review", "Five-stage review of a design by fresh-context reviewers."),
    ("egm-review-loop", "Iterate Goldfish review rounds until findings converge."),
    ("egm-resolve", "Triage review findings and apply approved fixes."),
    ("egm-bootstrap", "Generate README contracts across a codebase tree."),
    ("egm-lite", "Fast EGM loop for small changes: critique, plan, review."),
    ("egm-drift-report", "Find where checked-in docs diverge from the code."),
)


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    sub = parser.add_subparsers(dest="egm_command", metavar="<command>")

    sub.add_parser("path", help="Print the plugin root directory.")
    sub.add_parser("personas", help="List reviewer personas and their rubrics.")
    sub.add_parser("standards", help="List document standards.")
    sub.add_parser("stages", help="List the five review stages and their reviewers.")
    sub.add_parser("selftest", help="Check shipped skills, personas, standards and templates.")

    p_plan = sub.add_parser("plan", help="Show the semantic split and stage plan for a document.")
    p_plan.add_argument("target")
    p_plan.add_argument("--preset", default="full")
    p_plan.add_argument("--stage", default="")

    p_lint = sub.add_parser("lint", help="Deterministic document checks (no model calls).")
    p_lint.add_argument("target")
    p_lint.add_argument("--standard", default="feature-spec")
    p_lint.add_argument("--max-line-length", type=int, default=0)

    p_drift = sub.add_parser("drift", help="Verify document mentions against the code.")
    p_drift.add_argument("target")
    p_drift.add_argument("--no-write", action="store_true", help="Print the report instead of writing it.")

    p_boot = sub.add_parser("bootstrap", help="Discovery, pruning and partition plan.")
    p_boot.add_argument("path", nargs="?", default=".")
    p_boot.add_argument("--target-filename", default="README.md")
    p_boot.add_argument("--max-workers", type=int, default=5)

    p_status = sub.add_parser("status", help="Review-loop state for a document.")
    p_status.add_argument("target")

    p_review = sub.add_parser("review", help="Explain how to run a review (needs a chat session).")
    p_review.add_argument("target")
    p_review.add_argument("--stage", default="")
    p_review.add_argument("--preset", default="full")


def handle(args: argparse.Namespace) -> int:
    command = getattr(args, "egm_command", None)
    want_json = bool(getattr(args, "json", False))
    if not command:
        _print_help()
        return 0
    try:
        if command == "path":
            print(catalog.PLUGIN_ROOT)
            return 0
        if command == "personas":
            return _emit(want_json, catalog.inventories()["reviewers"], _format_personas)
        if command == "standards":
            return _emit(want_json, catalog.inventories()["standards"], _format_standards)
        if command == "stages":
            return _emit(want_json, catalog.inventories()["stages"], _format_stages)
        if command == "selftest":
            report = selftest()
            return _emit(want_json, report, lambda r: _format_selftest(r))
        if command == "plan":
            return _plan(args, want_json)
        if command == "lint":
            result = run_lint(args.target, args.standard, args.max_line_length)
            return _emit(want_json, result, _format_lint)
        if command == "drift":
            return _drift(args, want_json)
        if command == "bootstrap":
            plan = build_plan(args.path, target_filename=args.target_filename,
                              max_workers=args.max_workers)
            return _emit(want_json, plan, _format_plan)
        if command == "status":
            state = review.quick_status(args.target)
            return _emit(want_json, state, _format_status)
        if command == "review":
            return _review_hint(args)
    except EGMError as exc:
        print(f"egm: {exc}", file=sys.stderr)
        return 2
    print(f"egm: unknown command {command!r}", file=sys.stderr)
    return 2


# ── commands ──────────────────────────────────────────────────────────────

def _plan(args: argparse.Namespace, want_json: bool) -> int:
    options = review.ReviewOptions(preset=args.preset, stage=args.stage)
    doc, split, plans = review.plan_review(args.target, options)
    payload = {
        "target": str(doc.path),
        "line_count": doc.line_count,
        "split": split.as_dict(),
        "stages": [p.as_dict() for p in plans],
    }
    if want_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(f"Target: {doc.path} ({doc.line_count} lines)")
    feature = split.feature
    detailed = split.detailed
    print(f"Feature range: {feature.label}" + (f"   Detailed range: {detailed.label}" if detailed else
                                                "   Detailed range: N/A"))
    if split.ambiguous:
        print("Ambiguous split candidates: " + ", ".join(s.label for s in split.ambiguous))
    print()
    for plan in plans:
        if plan.skipped or plan.line_range is None:
            print(f"  Stage {plan.stage.number} {plan.stage.title}: SKIPPED — {plan.note}")
            continue
        names = ", ".join(spec.name for spec in plan.reviewers)
        print(f"  Stage {plan.stage.number} {plan.stage.title}: {plan.line_range.label} "
              f"[{names}] -> {plan.stage.report_name}")
    return 0


def _drift(args: argparse.Namespace, want_json: bool) -> int:
    path = resolve_target(args.target)
    root = path if path.is_dir() else path.parent
    files = drift_documents(path)
    scans = []
    for file in files:
        doc = documents.read_document(file)
        scans.append(scan_document(doc, root))
    import datetime as dt
    today = dt.date.today().isoformat()
    body = render_report(scans, date=today)
    if want_json:
        print(json.dumps({"date": today, "documents": [s.as_dict() for s in scans]},
                         indent=2, ensure_ascii=False))
        return 0
    if args.no_write:
        print(body)
        return 0
    target_dir = root
    output = target_dir / report_filename(today)
    output.write_text(body.rstrip() + "\n", encoding="utf-8")
    missing = sum(len(s.missing) for s in scans)
    print(f"{len(scans)} document(s), {missing} unverified mention(s). Report: {output}")
    return 0


def _review_hint(args: argparse.Namespace) -> int:
    stage = f" --stage {args.stage}" if args.stage else ""
    print("A Goldfish review needs fresh-context children, so it runs inside a chat session:\n")
    print(f"  /egm:egm-review {args.target}{stage}")
    print(f"  /egm-review {args.target}{stage}        (bare alias, if the host exposes plugin commands)\n")
    print("Or ask the agent to call egm_review with those arguments.")
    print("Run `hermes egm plan <target>` first to see the split and stage plan.")
    return 0


# ── selftest ──────────────────────────────────────────────────────────────

def selftest() -> dict[str, Any]:
    """Verify every shipped asset is present and internally consistent."""
    problems: list[str] = []
    skills = []
    for name, description in SKILLS:
        path = catalog.PLUGIN_ROOT / "skills" / name / "SKILL.md"
        if not path.exists():
            problems.append(f"missing skill: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        head = text.split("---", 2)
        if len(head) < 3 or not text.startswith("---"):
            problems.append(f"{name}: SKILL.md has no frontmatter")
        else:
            if f"name: {name}" not in head[1]:
                problems.append(f"{name}: frontmatter name does not match directory")
            if "description:" not in head[1]:
                problems.append(f"{name}: frontmatter has no description")
        if len(description) > 60:
            problems.append(f"{name}: description exceeds the 60-char index limit")
        skills.append({"name": name, "lines": text.count("\n") + 1, "description": description})
    for spec in catalog.inventories()["reviewers"]:
        persona = catalog.PERSONA_DIR / f"{spec['persona'].split('/')[-1]}"
        if not persona.exists():
            problems.append(f"missing persona: {persona}")
        if spec["standard"] and not (catalog.STANDARD_DIR / f"{spec['standard']}.md").exists():
            problems.append(f"missing standard: {spec['standard']}")
    for name in ("design_doc", "readme_templates"):
        if not (catalog.TEMPLATE_DIR / f"{name}.md").exists():
            problems.append(f"missing template: {name}")
    return {"ok": not problems, "problems": problems, "skills": skills,
            "reviewers": len(catalog.REVIEWER_SHORTNAMES),
            "standards": len(list(catalog.STANDARD_DIR.glob("*-standard.md")))}


# ── formatting ────────────────────────────────────────────────────────────

def _emit(want_json: bool, payload: Any, formatter: Any) -> int:
    if want_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(formatter(payload))
    return 0


def _format_personas(rows: list[dict[str, Any]]) -> str:
    lines = ["Reviewer          Role                          Standard", "-" * 78]
    for row in rows:
        lines.append(f"{row['name']:<17} {row['role']:<29} {row['standard'] or '—'}")
    return "\n".join(lines)


def _format_standards(rows: list[dict[str, Any]]) -> str:
    return "\n".join(f"{row['name']:<26} {row['path']}" for row in rows)


def _format_stages(rows: list[dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        lines.append(f"Stage {row['stage']}  {row['title']:<34} {row['range']:<8} "
                     f"{', '.join(row['reviewers'])} -> {row['report']}")
    return "\n".join(lines)


def _format_lint(result: dict[str, Any]) -> str:
    lines = [f"Standard: {result['standard']}"]
    for doc in result["documents"]:
        counts = doc["counts"]
        lines.append(f"\n{doc['path']}  (blocker {counts['blocker']}, major {counts['major']}, "
                     f"nit {counts['nit']})")
        for finding in doc["findings"]:
            lines.append(f"  [{finding['severity']:<7}] {finding['scope']}: {finding['description']}")
    if not result["findings"]:
        lines.append("\nNo findings.")
    return "\n".join(lines)


def _format_plan(plan: dict[str, Any]) -> str:
    lines = [f"Root: {plan['root']}",
             f"Verification: {plan['verification_command']}",
             f"Code directories: {plan['directory_count']}  max depth: {plan['max_depth']}",
             f"Partitions ({plan['partitions']['worker_count']}): "
             + ", ".join(plan["partitions"]["roots"]),
             f"Reason: {plan['partitions']['reason']}"]
    if plan.get("needs_safety_gate"):
        lines.append("SAFETY GATE: more than 15 directories — ask the user before spawning workers.")
    return "\n".join(lines)


def _format_status(state: dict[str, Any]) -> str:
    lines = [f"Target: {state['target'] or '(none recorded)'}",
             f"State file: {state['path']}",
             f"Settled findings: {state['settled']}  fixed scopes: {state['fixed_scopes']}"]
    for record in state["rounds"]:
        counts = record["counts"]
        lines.append(f"  round {record['round']}: {'clean' if record['clean'] else 'open'} "
                     f"(blocker {counts.get('blocker', 0)}, major {counts.get('major', 0)}, "
                     f"nit {counts.get('nit', 0)})")
    verdict = state["verdict"]
    lines.append(f"Verdict: {verdict['reason']}")
    return "\n".join(lines)


def _format_selftest(report: dict[str, Any]) -> str:
    lines = [("OK" if report["ok"] else "FAILED"),
             f"Skills: {len(report['skills'])}  reviewers: {report['reviewers']}  "
             f"standards: {report['standards']}"]
    for problem in report["problems"]:
        lines.append(f"  - {problem}")
    return "\n".join(lines)


def _print_help() -> None:
    print("hermes egm <command>\n")
    print("  path                 Print the plugin root")
    print("  personas             Reviewer personas and rubrics")
    print("  standards            Document standards")
    print("  stages               Five-stage pipeline")
    print("  plan <doc>           Semantic split and stage plan")
    print("  lint <path>          Deterministic document checks")
    print("  drift <path>         Verify document mentions against the code")
    print("  bootstrap [path]     README discovery and partition plan")
    print("  status <doc>         Review-loop state")
    print("  selftest             Check shipped assets")
    print("\nAdd --json to any command for machine-readable output.")
