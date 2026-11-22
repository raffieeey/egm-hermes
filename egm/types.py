"""Shared data model for the EGM plugin.

Every module imports its types from here so the engine, the tools, the CLI and
the tests agree on one vocabulary. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SEVERITIES: tuple[str, ...] = ("blocker", "major", "nit")
SEVERITY_RANK: dict[str, int] = {"blocker": 0, "major": 1, "nit": 2}
SEVERITY_LABEL: dict[str, str] = {"blocker": "🔴 Blocker", "major": "🟡 Major", "nit": "🟢 Nit"}
STATUS_COMPLETED = "completed"
STATUS_NOT_RUN = "not run"

_SEVERITY_ALIASES = {
    "blocker": "blocker", "blockers": "blocker", "critical": "blocker", "p0": "blocker",
    "major": "major", "high": "major", "p1": "major",
    "nit": "nit", "nits": "nit", "minor": "nit", "polish": "nit", "p2": "nit", "p3": "nit",
    "low": "nit",
}


class EGMError(Exception):
    """Base error for this plugin."""


class DocumentError(EGMError):
    """The target document is missing, unreadable, or a semantic target is unresolved."""


def normalize_severity(value: Any) -> str:
    """Map any reviewer spelling onto one of ``blocker | major | nit``.

    Unknown values become ``major``: a finding that cannot be classified is
    actionable, and silently dropping it would lose a reviewer's report.
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"^[^\w]+", "", text)
    text = re.sub(r"[^\w].*$", "", text)
    return _SEVERITY_ALIASES.get(text, "major")


@dataclass(frozen=True)
class LineRange:
    """An inclusive 1-based line range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < 1:
            raise ValueError("line range must be 1-based")
        if self.end < self.start:
            raise ValueError(f"line range end {self.end} precedes start {self.start}")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def label(self) -> str:
        return f"L{self.start}" if self.start == self.end else f"L{self.start}-L{self.end}"

    def clamp(self, total_lines: int) -> "LineRange":
        end = max(1, min(self.end, total_lines))
        start = max(1, min(self.start, end))
        return LineRange(start, end)

    def as_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}

    @classmethod
    def parse(cls, text: str) -> "LineRange | None":
        """Parse ``L10-L40``, ``10-40``, ``lines 10..40``, ``L7`` or ``10``."""
        if not text:
            return None
        raw = str(text).strip()
        numbers = re.findall(r"\d+", raw)
        if not numbers:
            return None
        if len(numbers) == 1:
            n = int(numbers[0])
            return cls(n, n)
        start, end = int(numbers[0]), int(numbers[1])
        if start > end:
            start, end = end, start
        return cls(start, end)


@dataclass(frozen=True)
class Section:
    """A markdown heading and the lines it owns, up to the next same-or-higher heading."""

    level: int
    title: str
    start: int
    end: int
    raw: str

    @property
    def label(self) -> str:
        return f"{'#' * self.level} {self.title} ({LineRange(self.start, self.end).label})"

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "title": self.title, "start": self.start,
                "end": self.end, "label": self.label}


@dataclass(frozen=True)
class Document:
    """A parsed markdown document: text, lines and heading tree."""

    path: Path
    text: str
    lines: tuple[str, ...]
    sections: tuple[Section, ...]

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def full_range(self) -> LineRange:
        return LineRange(1, max(1, self.line_count))

    def slice(self, rng: LineRange | None = None, *, numbered: bool = True) -> str:
        """Return the requested lines, 1-based, optionally prefixed ``N| ``."""
        rng = (rng or self.full_range).clamp(self.line_count)
        chunk = self.lines[rng.start - 1:rng.end]
        if not numbered:
            return "\n".join(chunk)
        width = len(str(rng.end))
        return "\n".join(f"{str(rng.start + i).rjust(width)}| {line}"
                         for i, line in enumerate(chunk))

    def find_sections(self, query: str) -> list[Section]:
        """Sections matching a heading query (case-insensitive, exact then prefix then contains)."""
        q = query.strip().lstrip("#").strip().lower()
        if not q:
            return []
        exact = [s for s in self.sections if s.title.strip().lower() == q]
        if exact:
            return exact
        prefix = [s for s in self.sections if s.title.strip().lower().startswith(q)]
        if prefix:
            return prefix
        return [s for s in self.sections if q in s.title.strip().lower()]

    def resolve(self, semantic: str) -> LineRange:
        """Resolve a semantic target string to a line range.

        Accepted forms, in order: empty / ``full file`` / ``*``; an explicit
        line range; a heading (with or without ``#``); an identifier such as
        ``S1.12`` or ``#S1.12`` that appears in a heading; or a quoted phrase
        inside a longer instruction.
        """
        text = (semantic or "").strip()
        if not text or text.lower() in {"full file", "full", "all", "*", "whole file"}:
            return self.full_range
        explicit = LineRange.parse(text) if _looks_like_range(text) else None
        if explicit is not None:
            return explicit.clamp(self.line_count)
        matches = self.find_sections(text)
        if len(matches) == 1:
            return LineRange(matches[0].start, matches[0].end)
        if len(matches) > 1:
            listing = "; ".join(s.label for s in matches[:6])
            raise DocumentError(
                f"Semantic target {semantic!r} matches {len(matches)} sections: {listing}. "
                "Use a line range or a more specific heading.")
        quoted = re.findall(r"[\"'`]([^\"'`]{2,})[\"'`]", text)
        for candidate in quoted:
            matches = self.find_sections(candidate)
            if len(matches) == 1:
                return LineRange(matches[0].start, matches[0].end)
        tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,}", text)
                  if not t.lower().startswith(("h1", "h2", "h3", "h4", "h5", "h6", "block", "section"))]
        for token in sorted(tokens, key=len, reverse=True):
            matches = self.find_sections(token)
            if len(matches) == 1:
                return LineRange(matches[0].start, matches[0].end)
        raise DocumentError(f"Cannot resolve semantic target {semantic!r} in {self.path.name}.")

    def heading_path(self, line: int) -> str:
        """The nearest enclosing heading for a line ("" when the line precedes every heading)."""
        best = ""
        for section in self.sections:
            if section.start <= line <= section.end:
                best = f"{'#' * section.level} {section.title}"
        return best

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "line_count": self.line_count,
                "sections": [s.as_dict() for s in self.sections]}


_RANGE_RE = re.compile(
    r"^(?:lines?\s*|l\s*)?(\d+)\s*(?:[-–—]{1,2}|\.\.+)\s*(?:l\s*|lines?\s*)?(\d+)$",
    re.IGNORECASE)
_SINGLE_RE = re.compile(r"^(?:lines?\s*|l\s*)?(\d+)$", re.IGNORECASE)


def _looks_like_range(text: str) -> bool:
    """True only for an explicit line-range expression.

    Deliberately strict: an identifier such as ``S1.12`` or a heading that
    happens to contain digits must reach the heading resolver, not be read as
    "lines 1 to 12". Accepts ``7``, ``L7``, ``L10-L40``, ``10..40``,
    ``lines 10 to 40``.
    """
    candidate = text.strip()
    return bool(_RANGE_RE.match(candidate) or _SINGLE_RE.match(candidate))


@dataclass(frozen=True)
class Split:
    """The semantic split between a feature specification and its implementation plan."""

    feature: LineRange
    detailed: LineRange | None
    boundary_line: int | None = None
    boundary_title: str = ""
    ambiguous: tuple[Section, ...] = ()

    @property
    def has_detailed(self) -> bool:
        return self.detailed is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature.as_dict(),
            "detailed": self.detailed.as_dict() if self.detailed else None,
            "boundary_line": self.boundary_line,
            "boundary_title": self.boundary_title,
            "ambiguous": [s.label for s in self.ambiguous],
        }


@dataclass(frozen=True)
class ReviewerSpec:
    """One reviewer: persona file plus the standard it audits against."""

    name: str
    persona: str
    standard: str | None
    role: str
    lens: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "persona": self.persona,
                "standard": self.standard, "role": self.role, "lens": self.lens}


@dataclass(frozen=True)
class Finding:
    """A single normalized reviewer finding."""

    reviewer: str
    severity: str
    scope: str
    description: str
    rationale: str = ""
    suggestion: str = ""
    id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "reviewer": self.reviewer, "severity": self.severity,
                "scope": self.scope, "description": self.description,
                "rationale": self.rationale, "suggestion": self.suggestion}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, reviewer: str) -> "Finding":
        return cls(
            reviewer=str(data.get("reviewer") or reviewer).strip(),
            severity=normalize_severity(data.get("severity")),
            scope=str(data.get("scope") or data.get("lines") or "").strip(),
            description=str(data.get("description") or data.get("finding") or "").strip(),
            rationale=str(data.get("rationale") or data.get("recommendation") or "").strip(),
            suggestion=str(data.get("suggestion") or data.get("proposed_edit") or "").strip(),
        )


@dataclass(frozen=True)
class ReviewerOutcome:
    """The terminal state of one reviewer child."""

    spec: ReviewerSpec
    status: str
    findings: tuple[Finding, ...] = ()
    raw: str = ""
    error: str = ""
    attempts: int = 1
    chunks: int = 1
    elapsed: float = 0.0

    @property
    def completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {"reviewer": self.spec.name, "role": self.spec.role, "status": self.status,
                "findings": [f.as_dict() for f in self.findings], "error": self.error,
                "attempts": self.attempts, "chunks": self.chunks,
                "elapsed": round(self.elapsed, 2)}


@dataclass(frozen=True)
class StageReport:
    """One stage of the five-stage review pipeline and what it produced."""

    stage: int
    key: str
    title: str
    path: str
    outcomes: tuple[ReviewerOutcome, ...] = ()
    findings: tuple[Finding, ...] = ()
    body: str = ""
    skipped: bool = False
    note: str = ""

    @property
    def completed_reviewers(self) -> tuple[str, ...]:
        return tuple(o.spec.name for o in self.outcomes if o.completed)

    @property
    def failed_reviewers(self) -> tuple[str, ...]:
        return tuple(o.spec.name for o in self.outcomes if not o.completed)

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "key": self.key, "title": self.title, "path": self.path,
                "skipped": self.skipped, "note": self.note,
                "counts": severity_counts(self.findings),
                "reviewers": [o.as_dict() for o in self.outcomes]}


@dataclass
class ReviewReport:
    """Everything a review run produced."""

    target: str
    preset: str
    split: Split | None = None
    stages: list[StageReport] = field(default_factory=list)
    consolidated_path: str = ""
    findings: tuple[Finding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "preset": self.preset,
            "split": self.split.as_dict() if self.split else None,
            "stages": [s.as_dict() for s in self.stages],
            "consolidated_path": self.consolidated_path,
            "counts": severity_counts(self.findings),
        }


def severity_counts(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def sort_findings(findings: Sequence[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.reviewer, f.scope))
