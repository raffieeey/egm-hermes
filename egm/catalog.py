"""Reviewer, standard and stage registry.

This is the single place that knows which persona audits against which rubric
and which five-stage pipeline the upstream EGM plugin defines. Skills, tools
and the CLI all read the same tables, so a stage cannot drift between surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import EGMError, ReviewerSpec

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PERSONA_DIR = PLUGIN_ROOT / "personas"
STANDARD_DIR = PLUGIN_ROOT / "standards"
TEMPLATE_DIR = PLUGIN_ROOT / "templates"

REVIEWER_SHORTNAMES: tuple[str, ...] = (
    "zoom_out", "comprehension", "design", "structural", "style",
    "implementation_readiness", "codebase_details", "inventive_catalyst",
)

_SPECS: dict[str, ReviewerSpec] = {
    "zoom_out": ReviewerSpec("zoom_out", "zoom_out", "zoom-out-standard",
                             "Skeptical Architect", "Simplicity, YAGNI & codebase reuse"),
    "comprehension": ReviewerSpec("comprehension", "comprehension", None,
                                  "Naive Reader", "Self-containment & clarity"),
    "design": ReviewerSpec("design", "design_critic", "feature-spec-standard",
                           "Design Critic", "Architecture, scale & failure modes"),
    "structural": ReviewerSpec("structural", "structural", "feature-spec-standard",
                               "Structural Validator", "Layout, headings & templates"),
    "style": ReviewerSpec("style", "style", None,
                          "Technical Copy Editor", "Clarity, precision & fluff"),
    "implementation_readiness": ReviewerSpec(
        "implementation_readiness", "implementation_readiness",
        "implementation-readiness-standard", "Implementation Readiness",
        "Actionability & zero-decision plans"),
    "codebase_details": ReviewerSpec("codebase_details", "codebase_details",
                                     "implementation-readiness-standard",
                                     "Codebase Details Auditor",
                                     "Docs vs. code accuracy & reuse"),
    "inventive_catalyst": ReviewerSpec("inventive_catalyst", "inventive_catalyst", None,
                                       "Inventive Catalyst",
                                       "TRIZ contradictions & ideal final result"),
}

#: Upstream aliases seen in prompts, persona frontmatter and user invocations.
_ALIASES: dict[str, str] = {
    "zoom-out": "zoom_out", "zoomout": "zoom_out", "egm-zoom-out": "zoom_out",
    "design_critic": "design", "egm-design": "design", "critic": "design",
    "naive_reader": "comprehension", "egm-comprehension": "comprehension",
    "structural": "structural", "egm-structural": "structural",
    "style": "style", "egm-style": "style",
    "readiness": "implementation_readiness", "egm-implementation-readiness": "implementation_readiness",
    "codebase": "codebase_details", "egm-codebase-details": "codebase_details",
    "inventive": "inventive_catalyst", "catalyst": "inventive_catalyst",
    "egm-inventive-catalyst": "inventive_catalyst",
}

#: The seven brainstorm lenses (upstream: egm-brainstorm "Goldfish Brainstorming Lenses").
BRAINSTORM_LENSES: tuple[str, ...] = (
    "design", "codebase_details", "zoom_out", "comprehension",
    "implementation_readiness", "structural", "inventive_catalyst",
)


@dataclass(frozen=True)
class Stage:
    """One stage of the five-stage Goldfish pipeline."""

    number: int
    key: str
    title: str
    reviewers: tuple[str, ...]
    range_key: str            # "full" | "feature" | "both" | "detailed"
    report_name: str
    uses_context: bool = True
    adds_report_to_context: str = ""


STAGES: tuple[Stage, ...] = (
    Stage(5, "zoom_out", "Initial Zoom Out Review", ("zoom_out", "inventive_catalyst"),
          "full", "zoom_out_report.md"),
    Stage(6, "comprehension", "Comprehension Test", ("comprehension",),
          "both", "comprehension_report.md", uses_context=False),
    Stage(7, "critic", "Critic Review", ("design", "structural", "style"),
          "feature", "critic_report.md"),
    Stage(8, "intermediate_zoom_out", "Intermediate Zoom Out",
          ("zoom_out",), "full", "intermediate_zoom_out_report.md"),
    Stage(9, "implementation_readiness", "Implementation Readiness Test",
          ("implementation_readiness", "codebase_details", "style"),
          "detailed", "implementation_readiness_report.md"),
)

_STAGE_BY_KEY = {stage.key: stage for stage in STAGES}
_STAGE_BY_NUMBER = {stage.number: stage for stage in STAGES}

PRESETS: dict[str, tuple[int, ...]] = {
    "interactive": (5, 6, 7, 8, 9),
    "consolidated": (5, 6, 7, 8, 9),
    "uninterrupted": (5, 6, 7, 8, 9),
    "feature_spec": (5, 6, 7, 8),
    "implementation": (9,),
}


def canonical_reviewer(name: str) -> str:
    """Map a reviewer shortname, alias, or persona filename onto a canonical name."""
    raw = str(name or "").strip().lower()
    if raw.endswith(".md"):
        raw = raw[:-3]
    raw = raw.replace("agents/", "").replace("personas/", "")
    if raw in _SPECS:
        return raw
    if raw in _ALIASES:
        return _ALIASES[raw]
    raise EGMError(f"Unknown reviewer {name!r}. Known reviewers: {', '.join(REVIEWER_SHORTNAMES)}.")


def reviewer(name: str, standard: str | None = None) -> ReviewerSpec:
    """Resolve a reviewer spec, optionally overriding its default standard."""
    canonical = canonical_reviewer(name)
    spec = _SPECS[canonical]
    if standard:
        resolved = canonical_standard(standard)
        if resolved != spec.standard:
            return ReviewerSpec(spec.name, spec.persona, resolved, spec.role, spec.lens)
    return spec


def canonical_standard(name: str) -> str:
    """Map a standard shortname or filename onto ``standards/<name>.md``."""
    raw = str(name or "").strip().lower()
    for suffix in (".md", "_standard"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    if raw.endswith("-standard"):
        raw = raw[: -len("-standard")]
    aliases = {
        "feature_spec": "feature-spec", "feature": "feature-spec", "spec": "feature-spec",
        "implementation_readiness": "implementation-readiness", "readiness": "implementation-readiness",
        "zoom_out": "zoom-out", "reimplementation": "re-implementation",
        "re_implementation": "re-implementation", "heilmeier_catechism": "heilmeier",
    }
    raw = aliases.get(raw, raw)
    path = STANDARD_DIR / f"{raw}-standard.md"
    if not path.exists():
        known = ", ".join(sorted(p.stem for p in STANDARD_DIR.glob("*.md")))
        raise EGMError(f"Unknown standard {name!r}. Known standards: {known}.")
    return f"{raw}-standard"


def stage(value: str | int) -> Stage:
    """Resolve a stage by number (5-9), key, or a leading digit in text."""
    raw = str(value or "").strip().lower()
    if raw.isdigit() and int(raw) in _STAGE_BY_NUMBER:
        return _STAGE_BY_NUMBER[int(raw)]
    if raw in _STAGE_BY_KEY:
        return _STAGE_BY_KEY[raw]
    for candidate in _STAGE_BY_KEY:
        if candidate in raw:
            return _STAGE_BY_KEY[candidate]
    raise EGMError(f"Unknown review stage {value!r}. Use 5-9 or one of {', '.join(_STAGE_BY_KEY)}.")


def preset_stages(preset: str) -> tuple[Stage, ...]:
    """Expand a preset name into its ordered stages."""
    key = str(preset or "interactive").strip().lower()
    if key in {"full", "all"}:
        key = "interactive"
    if key.isdigit():
        return (stage(key),)
    if key not in PRESETS:
        raise EGMError(f"Unknown preset {preset!r}. Use {', '.join(PRESETS)} or a stage 5-9.")
    return tuple(_STAGE_BY_NUMBER[n] for n in PRESETS[key])


def persona_text(name: str) -> str:
    """Read a persona file verbatim."""
    canonical = canonical_reviewer(name)
    path = PERSONA_DIR / f"{_SPECS[canonical].persona}.md"
    if not path.exists():
        raise EGMError(f"Persona file missing: {path}")
    return path.read_text(encoding="utf-8")


def standard_text(name: str) -> str:
    """Read a document standard verbatim."""
    path = STANDARD_DIR / f"{canonical_standard(name)}.md"
    if not path.exists():
        raise EGMError(f"Standard file missing: {path}")
    return path.read_text(encoding="utf-8")


def template_text(name: str) -> str:
    """Read a shipped template (``design_doc``, ``readme_templates``)."""
    path = TEMPLATE_DIR / f"{name}.md"
    if not path.exists():
        raise EGMError(f"Template missing: {path}")
    return path.read_text(encoding="utf-8")


def inventories() -> dict[str, Any]:
    """Everything the CLI and the ``/egm`` help card report about shipped assets."""
    return {
        "reviewers": [
            {"name": spec.name, "persona": f"personas/{spec.persona}.md",
             "standard": spec.standard, "role": spec.role, "lens": spec.lens}
            for spec in _SPECS.values()
        ],
        "stages": [
            {"stage": s.number, "key": s.key, "title": s.title,
             "reviewers": list(s.reviewers), "range": s.range_key, "report": s.report_name}
            for s in STAGES
        ],
        "standards": [
            {"name": p.stem.replace("-standard", ""), "path": f"standards/{p.name}"}
            for p in sorted(STANDARD_DIR.glob("*-standard.md"))
        ],
        "presets": {name: list(numbers) for name, numbers in PRESETS.items()},
    }
