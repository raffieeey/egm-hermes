"""Slash commands.

Hermes already turns every registered skill into ``/<plugin>:<skill>``, which is
the canonical entry point (``/egm:egm-review docs/x.md``) and starts a real turn
on every surface. The upstream plugin's commands have no ``egm:`` prefix, so this
module also registers bare aliases. An alias queues the same skill invocation into
the live session; where the host refuses injection it explains the namespaced
form instead of failing silently.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import catalog, documents, review
from .bootstrap import build_plan
from .lint import run_lint
from .reports import resolve_target
from .review import drift_documents
from .drift import render_report, report_filename, scan_document
from .types import EGMError

#: Bare alias -> plugin skill name.
ALIASES: dict[str, str] = {
    "egm-brainstorm": "egm-brainstorm",
    "egm-draft": "egm-draft",
    "egm-review": "egm-review",
    "egm-review-loop": "egm-review-loop",
    "egm-resolve": "egm-resolve",
    "egm-bootstrap": "egm-bootstrap",
    "egm-lite": "egm-lite",
    "egm-drift-report": "egm-drift-report",
}

_USAGE = {
    "egm-brainstorm": "<feature idea>",
    "egm-draft": "[context-file]",
    "egm-review": "<document> [stage 5-9]",
    "egm-review-loop": "<document> [semantic target]",
    "egm-resolve": "<document>",
    "egm-bootstrap": "<directory>",
    "egm-lite": "<task description>",
    "egm-drift-report": "<document or directory>",
}


def directive(skill: str, raw_args: str) -> str:
    """The message that activates a skill in the live session."""
    arguments = raw_args.strip()
    target = f'\nArguments from the user: {arguments}' if arguments else "\nNo arguments were supplied; ask for the one input the skill requires."
    return (
        f'Execute the EGM plugin skill `egm:{skill}`.\n\n'
        f'Call skill_view(name="egm:{skill}") first, then follow that skill exactly. '
        f'Use the plugin tools it names (egm_review, egm_review_loop, egm_brainstorm, egm_drift, '
        f'egm_lint, egm_bootstrap_plan, egm_status). Never paste a draft or report into chat: write '
        f'the artifact to disk and report its path.{target}'
    )


def make_alias(ctx: Any, skill: str) -> Callable[..., str]:
    """A slash-command handler that starts a skill, with an honest fallback."""

    def handler(raw_args: str = "", session_key: str = "", **_kwargs: Any) -> str:
        message = directive(skill, raw_args)
        injected = _inject(ctx, message, session_key)
        if injected:
            return f"Starting /{skill}."
        usage = _USAGE.get(skill, "")
        return (
            f"{message}\n\n"
            f"---\nThis host did not accept an injected turn. Run the namespaced form instead:\n"
            f"  /egm:{skill} {raw_args.strip() or usage}".rstrip()
        )

    return handler


def _inject(ctx: Any, message: str, session_key: str) -> bool:
    inject = getattr(ctx, "inject_message", None)
    if inject is None:
        return False
    try:
        return bool(inject(message, session_key=session_key or None))
    except TypeError:
        try:
            return bool(inject(message))
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def make_help(ctx: Any) -> Callable[..., str]:
    """``/egm`` — inventory, quick commands and the deterministic subcommands."""

    def handler(raw_args: str = "", **_kwargs: Any) -> str:
        argv = (raw_args or "").split()
        sub = argv[0].lower() if argv else "help"
        rest = " ".join(argv[1:])
        try:
            if sub in {"help", ""}:
                return _help_card()
            if sub == "personas":
                rows = catalog.inventories()["reviewers"]
                return "\n".join(f"{r['name']:<24} {r['role']:<30} {r['standard'] or '—'}"
                                 for r in rows)
            if sub == "standards":
                return "\n".join(f"{r['name']:<26} {r['path']}"
                                 for r in catalog.inventories()["standards"])
            if sub == "stages":
                rows = catalog.inventories()["stages"]
                return "\n".join(
                    f"{r['stage']}  {r['title']:<34} {r['range']:<8} "
                    f"{', '.join(r['reviewers'])} -> {r['report']}" for r in rows)
            if sub == "path":
                return str(catalog.PLUGIN_ROOT)
            if sub == "lint":
                if not rest:
                    return "Usage: /egm lint <path>"
                return _lint_text(run_lint(rest))
            if sub == "drift":
                if not rest:
                    return "Usage: /egm drift <document or directory>"
                return _drift_text(rest)
            if sub == "plan":
                if not rest:
                    return "Usage: /egm plan <document>"
                return _plan_text(rest)
            if sub == "bootstrap":
                plan = build_plan(rest or ".", target_filename="README.md", max_workers=5)
                return (f"{plan['directory_count']} code directories, partitions: "
                        f"{', '.join(plan['partitions']['roots'])} "
                        f"({plan['partitions']['reason']}); verification: {plan['verification_command']}")
            if sub == "status":
                if not rest:
                    return "Usage: /egm status <document>"
                state = review.quick_status(rest)
                return f"{state['path']}\n" + json.dumps(state["verdict"], indent=2)
            return _help_card() + f"\nUnknown subcommand: {sub}"
        except EGMError as exc:
            return f"egm: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"egm: {exc.__class__.__name__}: {exc}"

    return handler


def _help_card() -> str:
    lines = [
        "Elephant-Goldfish Model",
        "",
        "Start a workflow (also available namespaced as /egm:<skill>):",
        "  /egm-brainstorm <idea>        multi-lens concept review -> concepts_brief.md",
        "  /egm-draft [context]          section-by-section design draft",
        "  /egm-review <doc> [stage]     five-stage Goldfish review",
        "  /egm-review-loop <doc>        iterative rounds until convergence",
        "  /egm-resolve <doc>            triage findings, apply approved fixes",
        "  /egm-bootstrap <dir>          README contracts across a tree",
        "  /egm-lite <task>              fast critique -> checklist -> review",
        "  /egm-drift-report <path>      docs vs. code drift report",
        "",
        "Deterministic subcommands (no model calls):",
        "  /egm lint <path> | plan <doc> | drift <path> | status <doc>",
        "  /egm personas | standards | stages | path",
    ]
    return "\n".join(lines)


def _lint_text(result: dict[str, Any]) -> str:
    lines = [f"Lint ({result['standard']}):"]
    for doc in result["documents"]:
        counts = doc["counts"]
        lines.append(f"  {doc['path']}: {counts['blocker']} blocker, {counts['major']} major, "
                     f"{counts['nit']} nit")
        for finding in doc["findings"][:12]:
            lines.append(f"    [{finding['severity']}] {finding['scope']}: {finding['description']}")
    if not result["findings"]:
        lines.append("  No findings.")
    return "\n".join(lines)


def _drift_text(target: str) -> str:
    import datetime as dt
    path = resolve_target(target)
    root = path if path.is_dir() else path.parent
    files = drift_documents(path)
    scans = [scan_document(documents.read_document(f), root) for f in files]
    today = dt.date.today().isoformat()
    output = root / report_filename(today)
    output.write_text(render_report(scans, date=today).rstrip() + "\n", encoding="utf-8")
    missing = sum(len(s.missing) for s in scans)
    return f"Checked {len(scans)} document(s): {missing} unverified mention(s). Report: {output}"


def _plan_text(target: str) -> str:
    doc, split, plans = review.plan_review(target, review.ReviewOptions())
    lines = [f"{doc.path} ({doc.line_count} lines)",
             f"feature {split.feature.label}" +
             (f", detailed {split.detailed.label}" if split.detailed else ", no implementation plan")]
    for plan in plans:
        if plan.skipped or plan.line_range is None:
            lines.append(f"  stage {plan.stage.number} {plan.stage.title}: skipped — {plan.note}")
        else:
            lines.append(f"  stage {plan.stage.number} {plan.stage.title}: {plan.line_range.label} "
                         f"[{', '.join(s.name for s in plan.reviewers)}]")
    return "\n".join(lines)
