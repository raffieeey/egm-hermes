"""The plugin's tool surface.

Seven tools, each one a thin, typed door onto the engine:

- ``egm_review``          run a preset or one stage of the five-stage pipeline
- ``egm_review_loop``     run one iterative round and get a convergence verdict
- ``egm_brainstorm``      run the seven-lens concept swarm
- ``egm_drift``           compare documents with the code
- ``egm_lint``            deterministic document checks, no model calls
- ``egm_bootstrap_plan``  discovery, pruning and partitioning for README work
- ``egm_status``          loop state for a target

Handlers never raise into the agent loop: an expected failure comes back as
``{"ok": false, "error": "..."}`` so the caller can recover or report it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import catalog, review
from .bootstrap import build_plan
from .lint import run_lint
from .types import EGMError

logger = logging.getLogger(__name__)

TOOLSET = "egm"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _guard(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], str]:
    def handler(args: dict[str, Any]) -> str:
        try:
            return _json(fn(args or {}))
        except EGMError as exc:
            return _json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface, never crash the turn
            logger.exception("egm tool failed")
            return _json({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    return handler


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _as_standard_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _settings(ctx: Any) -> dict[str, Any]:
    return review.read_settings(ctx)


# ── handlers ──────────────────────────────────────────────────────────────

def _review(ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "").strip()
        if not target:
            raise EGMError("egm_review requires `target` (path to the design document).")
        options = review.ReviewOptions(
            preset=str(args.get("preset") or "full"),
            stage=str(args.get("stage") or ""),
            reviewers=tuple(catalog.canonical_reviewer(n) for n in _as_list(args.get("reviewers"))),
            standards=_as_standard_map(args.get("standards")),
            context=tuple(_as_list(args.get("context"))),
            write_reports=bool(args.get("write_reports", True)),
        )
        result = review.run_review(ctx, target, options, _settings(ctx))
        return {"ok": True, **result.as_dict(),
                "reports": [s.path for s in result.stages if s.path] +
                           ([result.consolidated_path] if result.consolidated_path else [])}
    return run


def _review_loop(ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "").strip()
        if not target:
            raise EGMError("egm_review_loop requires `target`.")
        reviewers = [catalog.canonical_reviewer(n) for n in _as_list(args.get("reviewers"))] or ["design"]
        return review.run_loop_round(
            ctx, target, reviewers=reviewers,
            standards=_as_standard_map(args.get("standards")),
            semantic_target=str(args.get("semantic_target") or "full file"),
            context=_as_list(args.get("context")),
            settings=_settings(ctx),
            max_rounds=int(args.get("max_rounds") or 5),
            report_name=str(args.get("report_name") or "goldfish_review_report.md"),
        )
    return run


def _brainstorm(ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        hypothesis = str(args.get("hypothesis") or "").strip()
        if not hypothesis:
            raise EGMError("egm_brainstorm requires `hypothesis`.")
        return review.run_brainstorm(
            ctx, hypothesis,
            reviewers=[catalog.canonical_reviewer(n) for n in _as_list(args.get("reviewers"))],
            context=_as_list(args.get("context")),
            out=str(args.get("out") or "concepts_brief.md"),
            author=str(args.get("author") or ""),
            settings=_settings(ctx),
            root=str(args.get("root") or "") or None,
        )
    return run


def _drift(ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "").strip()
        if not target:
            raise EGMError("egm_drift requires `target` (a document or a directory).")
        return review.run_drift(ctx, target, context=_as_list(args.get("context")),
                                verify=bool(args.get("verify", True)),
                                settings=_settings(ctx))
    return run


def _lint(_ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "").strip()
        if not target:
            raise EGMError("egm_lint requires `target`.")
        return run_lint(target, str(args.get("standard") or "feature-spec"),
                        int(args.get("max_line_length") or 0))
    return run


def _bootstrap(_ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or ".").strip() or "."
        plan = build_plan(path, target_filename=str(args.get("target_filename") or "README.md"),
                          max_workers=int(args.get("max_workers") or 5),
                          ignore=_as_list(args.get("ignore")))
        return {"ok": True, "plan": plan}
    return run


def _status(_ctx: Any):
    def run(args: dict[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "").strip()
        if not target:
            raise EGMError("egm_status requires `target`.")
        return review.quick_status(target)
    return run


# ── schemas ───────────────────────────────────────────────────────────────

_TARGET = {"type": "string", "description": "Path to the design document."}
_REVIEWERS = {"type": "array", "items": {"type": "string"},
              "description": ("Reviewer shortnames: zoom_out, comprehension, design, structural, "
                              "style, implementation_readiness, codebase_details, inventive_catalyst.")}
_STANDARDS = {"type": "object", "additionalProperties": {"type": "string"},
              "description": ("Optional reviewer -> standard overrides, e.g. "
                              "{\"design\": \"feature-spec-standard\"}.")}
_CONTEXT = {"type": "array", "items": {"type": "string"},
            "description": "Extra reading-list files (source files or saved pages)."}

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "egm_review",
        "emoji": "🐘",
        "description": (
            "Run the Elephant-Goldfish five-stage review on a design document with fresh-context "
            "reviewer subagents that never see this conversation. Stages: 5 zoom-out, 6 comprehension, "
            "7 critic, 8 intermediate zoom-out, 9 implementation readiness. Use `preset` for a full run "
            "(full), design-only (feature_spec), plan-only (implementation), or pass `stage` for one "
            "stage. Writes the stage reports and a consolidated report next to the target and returns "
            "their paths, severity counts and per-reviewer status."),
        "parameters": {
            "type": "object",
            "properties": {
                "target": _TARGET,
                "preset": {"type": "string",
                           "enum": ["full", "interactive", "consolidated", "feature_spec", "implementation"],
                           "description": "Execution plan. Defaults to full (all five stages)."},
                "stage": {"type": "string",
                          "description": "Run only this stage: 5-9 or a stage key. Overrides preset."},
                "reviewers": _REVIEWERS,
                "standards": _STANDARDS,
                "context": _CONTEXT,
                "write_reports": {"type": "boolean", "default": True},
            },
            "required": ["target"],
        },
    },
    {
        "name": "egm_review_loop",
        "emoji": "🐠",
        "description": (
            "Run exactly one round of the iterative Goldfish review loop on a document range, persist "
            "round state, and return findings plus a convergence verdict (clean, converged, cap_reached, "
            "churn). Call it again only after the user resolves findings and confirms another round; "
            "two consecutive clean rounds converge and the cap is five rounds."),
        "parameters": {
            "type": "object",
            "properties": {
                "target": _TARGET,
                "reviewers": _REVIEWERS,
                "standards": _STANDARDS,
                "semantic_target": {"type": "string",
                                    "description": "Line range, heading, or identifier. Defaults to full file."},
                "context": _CONTEXT,
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "report_name": {"type": "string", "default": "goldfish_review_report.md",
                                "description": "Artifact filename for this round's report."},
            },
            "required": ["target", "reviewers"],
        },
    },
    {
        "name": "egm_brainstorm",
        "emoji": "🧠",
        "description": (
            "Stress-test a feature concept with seven parallel fresh-context lenses (YAGNI, cost and "
            "quota, UX, migration, observability, tech debt, inventive contradiction resolution) and "
            "write a concepts_brief.md draft with the analysis filled in. Use before drafting a design "
            "document, after the author has anchored the problem statement."),
        "parameters": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string", "description": "The anchored concept, 2-5 sentences."},
                "reviewers": _REVIEWERS,
                "context": _CONTEXT,
                "out": {"type": "string", "default": "concepts_brief.md"},
                "author": {"type": "string"},
                "root": {"type": "string", "description": "Directory to write the brief into."},
            },
            "required": ["hypothesis"],
        },
    },
    {
        "name": "egm_drift",
        "emoji": "🧭",
        "description": (
            "Detect documentation drift: verify every path, symbol, endpoint and table a document "
            "mentions against the current source tree, then run fresh-context codebase auditors for "
            "semantic divergence. Writes a dated drift report and lists only the divergences."),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "A design document or a directory of them."},
                "context": _CONTEXT,
                "verify": {"type": "boolean", "default": True,
                           "description": "Also run fresh-context semantic reviewers."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "egm_lint",
        "emoji": "🧹",
        "description": (
            "Deterministic document checks with no model calls: placeholders, unclosed code fences, "
            "skipped heading levels, empty and duplicate sections, malformed tables, broken or escaping "
            "relative links, and the required sections for a document standard. Use it as a cheap first "
            "pass before a full Goldfish review."),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "A document or a directory of markdown."},
                "standard": {"type": "string", "default": "feature-spec",
                             "enum": ["feature-spec", "implementation-readiness", "zoom-out",
                                      "heilmeier", "re-implementation"]},
                "max_line_length": {"type": "integer", "default": 0,
                                    "description": "Flag lines longer than this; 0 disables."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "egm_bootstrap_plan",
        "emoji": "📚",
        "description": (
            "Deterministically plan egm-bootstrap: breadth-first directory discovery with .gitignore and "
            ".egmignore support, test-only and non-code pruning, the adaptive 3-5 way subtree "
            "partitioning, the verification command, and whether the >15-directory safety gate applies. "
            "Call this before spawning bootstrap workers so the tree math is not guessed."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root directory to document."},
                "target_filename": {"type": "string", "default": "README.md"},
                "max_workers": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
                "ignore": {"type": "array", "items": {"type": "string"},
                           "description": "Extra glob patterns to ignore."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "egm_status",
        "emoji": "📊",
        "description": (
            "Show recorded review-loop state for a document: rounds, severity counts, which reviewers "
            "ran, settled findings, and the current convergence verdict."),
        "parameters": {
            "type": "object",
            "properties": {"target": _TARGET},
            "required": ["target"],
        },
    },
)

_HANDLERS: dict[str, Callable[[Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "egm_review": _review,
    "egm_review_loop": _review_loop,
    "egm_brainstorm": _brainstorm,
    "egm_drift": _drift,
    "egm_lint": _lint,
    "egm_bootstrap_plan": _bootstrap,
    "egm_status": _status,
}


def iter_tool_registrations(ctx: Any) -> list[dict[str, Any]]:
    """Yield ``register_tool`` kwargs for every tool, bound to this context."""
    registrations: list[dict[str, Any]] = []
    for spec in TOOL_SPECS:
        builder = _HANDLERS[spec["name"]]
        registrations.append({
            "name": spec["name"],
            "toolset": TOOLSET,
            "schema": {"name": spec["name"], "description": spec["description"],
                       "parameters": spec["parameters"]},
            "handler": _guard(builder(ctx)),
            "description": spec["description"],
            "emoji": spec["emoji"],
        })
    return registrations


def tool_names() -> tuple[str, ...]:
    return tuple(spec["name"] for spec in TOOL_SPECS)


__all__ = ["TOOL_SPECS", "TOOLSET", "iter_tool_registrations", "tool_names"]
