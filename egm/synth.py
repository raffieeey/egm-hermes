"""Finding synthesis: parse reviewer payloads, normalize them, and render reports.

Reviewers answer in prose, JSON blobs or half-structured markdown. This module is
the single place that turns that noise into ``Finding`` objects and renders the
two deliverable layouts the plugin ships: the per-round review report and the
cross-stage consolidated report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .types import (
    SEVERITIES,
    SEVERITY_LABEL,
    SEVERITY_RANK,
    STATUS_COMPLETED,
    STATUS_NOT_RUN,
    Finding,
    LineRange,
    ReviewerOutcome,
    Split,
    StageReport,
    normalize_severity,
    severity_counts,
)

#: Longest description or rationale we keep, in characters.
MAX_TEXT = 4000
#: Longest finding title derived from a description.
TITLE_LIMIT = 72
#: Jaccard similarity at or above which two descriptions are the same finding.
DEDUPE_THRESHOLD = 0.8

TEXT_KEYS: tuple[str, ...] = (
    "description", "finding", "issue", "finding_description", "issue_description",
    "text", "detail", "problem", "concern", "observation", "summary", "message",
)
SEVERITY_KEYS: tuple[str, ...] = ("severity", "priority", "level", "rating", "classification")
SCOPE_KEYS: tuple[str, ...] = (
    "scope", "lines", "line_range", "line", "location", "section", "area", "where", "target",
)
RATIONALE_KEYS: tuple[str, ...] = (
    "rationale", "recommendation", "reason", "why", "impact", "justification", "explanation",
)
SUGGESTION_KEYS: tuple[str, ...] = (
    "suggestion", "proposed_edit", "proposed_fix", "fix", "edit", "remediation", "action",
)
REVIEWER_KEYS: tuple[str, ...] = ("reviewer", "agent", "persona", "lens", "author")
CONTAINER_KEYS: tuple[str, ...] = (
    "findings", "issues", "results", "items", "observations", "comments", "problems",
)
NESTED_KEYS: tuple[str, ...] = ("report", "result", "data", "review", "payload", "output")

_WS_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+.-]*[ \t]*\r?\n(.*?)(?:\r?\n)?```", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #

def _clean_text(value: Any) -> str:
    """Collapse all whitespace runs in ``value`` to single spaces and strip."""
    text = "" if value is None else str(value)
    return _WS_RE.sub(" ", text).strip()


def _cap(text: str, limit: int = MAX_TEXT) -> str:
    """Truncate ``text`` to at most ``limit`` characters."""
    return text if len(text) <= limit else text[:limit].rstrip()


def _id_prefix(reviewer: str) -> str:
    """Make a reviewer name safe for use inside a finding id."""
    return _ID_SAFE_RE.sub("-", _clean_text(reviewer)).strip("-") or "reviewer"


def _code(value: Any) -> str:
    """Render ``value`` inside an inline-code span without breaking it."""
    text = _clean_text(value).replace("`", "'")
    return text or "unspecified"


def _cell(value: Any) -> str:
    """Render one markdown table cell, escaping pipes."""
    return _clean_text(value).replace("|", "\\|")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render a pipe table with ``---`` separator rows."""
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(lines)


def _summary_label(severity: str) -> str:
    """Capitalized severity name used in tables."""
    return severity.capitalize()


# --------------------------------------------------------------------------- #
# Finding construction
# --------------------------------------------------------------------------- #

def _normalized_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Lower-case mapping keys into ``snake_case`` so aliases line up."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        name = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
        if name and name not in out:
            out[name] = value
    return out


def _first(values: Mapping[str, Any], keys: Sequence[str]) -> str:
    """Return the first non-empty scalar among ``keys``."""
    for key in keys:
        value = values.get(key)
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _is_finding(obj: Any) -> bool:
    """True when ``obj`` is a mapping carrying at least one text key."""
    if not isinstance(obj, Mapping):
        return False
    values = _normalized_keys(obj)
    return any(_first(values, (key,)) for key in TEXT_KEYS)


def _finding_from_mapping(data: Mapping[str, Any], reviewer: str) -> Finding:
    """Build a ``Finding`` from a loose JSON object, tolerating key aliases."""
    values = _normalized_keys(data)
    owner = _first(values, REVIEWER_KEYS) or reviewer
    return Finding(
        reviewer=owner,
        severity=normalize_severity(_first(values, SEVERITY_KEYS) or "major"),
        scope=_clean_text(_first(values, SCOPE_KEYS)),
        description=_cap(_clean_text(_first(values, TEXT_KEYS))),
        rationale=_cap(_clean_text(_first(values, RATIONALE_KEYS))),
        suggestion=_cap(_clean_text(_first(values, SUGGESTION_KEYS))),
    )


def _extract(obj: Any, reviewer: str, depth: int = 0) -> list[Finding]:
    """Recursively pull findings out of a decoded JSON value."""
    if depth > 8:
        return []
    if isinstance(obj, list):
        found: list[Finding] = []
        for item in obj:
            if isinstance(item, Mapping) and _is_finding(item):
                found.append(_finding_from_mapping(item, reviewer))
            elif isinstance(item, (list, Mapping)):
                found.extend(_extract(item, reviewer, depth + 1))
        return found
    if isinstance(obj, Mapping):
        for key in NESTED_KEYS:
            nested = obj.get(key)
            if isinstance(nested, (Mapping, list)):
                found = _extract(nested, reviewer, depth + 1)
                if found:
                    return found
        for key in CONTAINER_KEYS:
            nested = obj.get(key)
            if isinstance(nested, list):
                found = [f for f in (_as_finding(i, reviewer) for i in nested) if f is not None]
                if found:
                    return found
            if isinstance(nested, Mapping):
                found = _extract(nested, reviewer, depth + 1)
                if found:
                    return found
        if _is_finding(obj):
            return [_finding_from_mapping(obj, reviewer)]
        # Last resort: a mapping keyed by reviewer name, e.g. {"design": [...]}.
        found = []
        for value in obj.values():
            if isinstance(value, (Mapping, list)):
                found.extend(_extract(value, reviewer, depth + 1))
        return found
    return []


def _declares_findings(obj: Any, depth: int = 0) -> bool:
    """True when a JSON value is shaped like a findings container, even if empty.

    ``{"findings": []}`` is a completed review with nothing to report. Without
    this check the empty list looks identical to "no JSON found" and a clean
    reviewer would be misreported as not run.
    """
    if depth > 8:
        return False
    if isinstance(obj, list):
        return all(isinstance(item, Mapping) for item in obj)
    if isinstance(obj, Mapping):
        if _is_finding(obj):
            return True
        return any(isinstance(obj.get(key), (list, Mapping))
                   for key in (*CONTAINER_KEYS, *NESTED_KEYS))
    return False


def _as_finding(item: Any, reviewer: str) -> Finding | None:
    """Coerce one list element into a finding, or ``None`` when it is not one."""
    return _finding_from_mapping(item, reviewer) if _is_finding(item) else None


def _prepare(finding: Finding, default_reviewer: str = "reviewer") -> Finding:
    """Clean one finding: strip text, normalize severity, cap long fields."""
    return Finding(
        reviewer=_clean_text(finding.reviewer) or _clean_text(default_reviewer) or "reviewer",
        severity=normalize_severity(finding.severity),
        scope=_clean_text(finding.scope),
        description=_cap(_clean_text(finding.description)),
        rationale=_cap(_clean_text(finding.rationale)),
        suggestion=_cap(_clean_text(finding.suggestion)),
        id=_clean_text(finding.id),
    )


def _assign_ids(findings: Sequence[Finding], *, round_no: int = 1) -> list[Finding]:
    """Assign ``<reviewer>-r<round>-<n>`` ids in the given order."""
    rnd = max(1, int(round_no))
    return [replace(f, id=f"{_id_prefix(f.reviewer)}-r{rnd}-{n}")
            for n, f in enumerate(findings, start=1)]


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #

def _repair(text: str) -> str:
    """Drop trailing commas before ``}``/``]`` outside of strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look < len(text) and text[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _strict_loads(text: str) -> list[Any]:
    """Decode ``text`` strictly, then once more with trailing commas repaired."""
    values: list[Any] = []
    stripped = text.strip()
    if not stripped:
        return values
    for candidate in (stripped, _repair(stripped)):
        try:
            values.append(json.loads(candidate))
        except (ValueError, TypeError):
            continue
    return values


def _fenced_blocks(text: str) -> list[str]:
    """Return the bodies of every fenced code block, in document order."""
    return [match.group(1) for match in _FENCE_RE.finditer(text)]


def _balanced(text: str, start: int) -> str | None:
    """Return the balanced JSON object/array starting at ``start``, if any."""
    opener = text[start]
    if opener not in "{[":
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _recover(payload: str, reviewer: str) -> list[Finding]:
    """Recover findings from a payload using the documented tolerance order."""
    text = str(payload or "")
    if not text.strip():
        raise ValueError(
            f"No usable findings in the {reviewer!r} payload: the payload is empty. "
            "Expected a JSON array, a {\"findings\": [...]} object, or a finding object "
            "with a description."
        )

    # (a) The whole payload is JSON.
    for candidate in _strict_loads(text):
        found = _extract(candidate, reviewer)
        if found or _declares_findings(candidate):
            return found

    # (b) A fenced ```json block (first one that yields findings).
    for block in _fenced_blocks(text):
        for candidate in _strict_loads(block):
            found = _extract(candidate, reviewer)
            if found or _declares_findings(candidate):
                return found

    # (c) Balanced JSON values found by scanning for '{' or '['. A reviewer may
    # emit several top-level values (JSONL, prose between objects), so collect
    # every non-overlapping value instead of stopping at the first one.
    decoder = json.JSONDecoder()
    scanned: list[Finding] = []
    declared = False
    covered = 0
    for match in re.finditer(r"[\[{]", text):
        if match.start() < covered:
            continue
        try:
            value, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        covered = max(covered, end)
        declared = declared or _declares_findings(value)
        scanned.extend(_extract(value, reviewer))
    if scanned or declared:
        return scanned

    # (d) Salvage finding objects one by one from prose.
    collected: list[Finding] = []
    salvaged = False
    covered = 0
    for match in re.finditer(r"\{", text):
        if match.start() < covered:
            continue
        chunk = _balanced(text, match.start())
        if not chunk:
            continue
        try:
            value = json.loads(_repair(chunk))
        except ValueError:
            continue
        covered = match.start() + len(chunk)
        salvaged = salvaged or _declares_findings(value)
        collected.extend(_extract(value, reviewer))
    if collected or salvaged:
        return collected

    raise ValueError(
        f"No usable findings in the {reviewer!r} payload. Expected a JSON array, "
        "a {\"findings\": [...]} object, a ```json block, or a single finding object "
        "with a description/finding/issue key."
    )


def parse_findings(payload: str, *, reviewer: str) -> list[Finding]:
    """Parse a reviewer payload into normalized, de-duplicated findings."""
    raw = _recover(payload, reviewer)
    prepared = [f for f in (_prepare(f, reviewer) for f in raw) if f.description]
    if not prepared:
        if not raw:
            # A reviewer that reports `{"findings": []}` has completed its review
            # and found nothing. That is a valid, expected result, not a failure.
            return []
        raise ValueError(
            f"No usable findings in the {reviewer!r} payload: every recovered object "
            "was missing a non-empty description."
        )
    return _assign_ids(dedupe(prepared))


# --------------------------------------------------------------------------- #
# Normalization and de-duplication
# --------------------------------------------------------------------------- #

def normalize(findings: Iterable[Finding], *, reviewer: str, round_no: int = 1) -> list[Finding]:
    """Clean findings, sort by severity, and assign stable ``<reviewer>-r<n>-<n>`` ids."""
    prepared = [f for f in (_prepare(f, reviewer) for f in findings) if f.description]
    ordered = [f for _, f in sorted(
        enumerate(prepared), key=lambda pair: (SEVERITY_RANK.get(pair[1].severity, 9), pair[0]))]
    return _assign_ids(ordered, round_no=round_no)


def _tokens(text: str) -> set[str]:
    """Casefolded word tokens used for similarity checks."""
    return set(_TOKEN_RE.findall(text.casefold()))


def _jaccard(left: str, right: str) -> float:
    """Jaccard similarity of two word-token sets."""
    a, b = _tokens(left), _tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _scope_key(scope: str) -> str:
    """Normalize a scope so ``L10-L20``, ``lines 10-20`` and ``10..20`` agree."""
    text = _clean_text(scope).casefold()
    if not text:
        return ""
    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\blines?\b", " ", text)
    text = re.sub(r"(?<![a-z])l\s*(?=\d)", "", text)
    text = re.sub(r"(\d)\s*(?:\.\.+|through|thru|to|-)\s*(\d)", r"\1-\2", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\b(\d+)-(\d+)\b",
                  lambda m: m.group(1) if m.group(1) == m.group(2)
                  else f"{m.group(1)}-{m.group(2)}", text)
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"[^\w\s:/.-]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -:")


def _join_text(first: str, second: str) -> str:
    """Join two non-empty pieces of text with ``" | "``, without duplicating."""
    left = _cap(_clean_text(first))
    right = _cap(_clean_text(second))
    if not left:
        return right
    if not right or left == right or right in left:
        return left
    if left in right:
        return right
    return _cap(f"{left} | {right}")


def _append_note(text: str, note: str) -> str:
    """Append a bookkeeping note to text, keeping it visible under the cap."""
    base = _cap(_clean_text(text))
    if not base:
        return note
    if note.casefold() in base.casefold():
        return base
    combined = f"{base} | {note}"
    if len(combined) <= MAX_TEXT:
        return combined
    keep = max(0, MAX_TEXT - len(note) - 3)
    return f"{base[:keep].rstrip()} | {note}"


def _merge_pair(keep: Finding, other: Finding) -> Finding:
    """Merge a duplicate into the earlier finding, upgrading its severity."""
    severity = (other.severity
                if SEVERITY_RANK.get(other.severity, 9) < SEVERITY_RANK.get(keep.severity, 9)
                else keep.severity)
    return Finding(
        reviewer=keep.reviewer,
        severity=severity,
        scope=keep.scope or other.scope,
        description=keep.description,
        rationale=_join_text(keep.rationale, other.rationale),
        suggestion=_join_text(keep.suggestion, other.suggestion),
        id=keep.id or other.id,
    )


def _same_group(left: Finding, right: Finding, right_key: str | None = None) -> bool:
    """True when two findings share reviewer, scope and a near-identical description."""
    if left.reviewer.casefold() != right.reviewer.casefold():
        return False
    left_key = _scope_key(left.scope)
    other_key = _scope_key(right.scope) if right_key is None else right_key
    if left_key != other_key:
        return False
    return _jaccard(left.description, right.description) >= DEDUPE_THRESHOLD


def _find_match(survivors: Sequence[Finding], candidate: Finding, key: str) -> int | None:
    """Index of the first survivor that duplicates ``candidate``, if any."""
    for index, survivor in enumerate(survivors):
        if _same_group(survivor, candidate, key):
            return index
    return None


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Collapse near-identical findings from the same reviewer and scope."""
    prepared = [f for f in (_prepare(f) for f in findings) if f.description]
    survivors: list[Finding] = []
    for candidate in prepared:
        key = _scope_key(candidate.scope)
        index = _find_match(survivors, candidate, key)
        if index is None:
            survivors.append(candidate)
        else:
            survivors[index] = _merge_pair(survivors[index], candidate)
    return survivors


def counts(findings: Iterable[Finding]) -> dict[str, int]:
    """Count findings per severity, always returning all three keys."""
    return severity_counts(findings)


# --------------------------------------------------------------------------- #
# Review report
# --------------------------------------------------------------------------- #

def _status(outcome: ReviewerOutcome) -> str:
    """Map an outcome onto the two statuses the report may print."""
    return STATUS_COMPLETED if outcome.completed else STATUS_NOT_RUN


def _prioritized(findings: Iterable[Finding]) -> list[Finding]:
    """Clean findings and order them Blocker -> Major -> Nit, stable within a rank."""
    prepared = [f for f in (_prepare(f) for f in findings) if f.description]
    return [f for _, f in sorted(
        enumerate(prepared), key=lambda pair: (SEVERITY_RANK.get(pair[1].severity, 9), pair[0]))]


def _short_title(description: str, *, limit: int = TITLE_LIMIT) -> str:
    """Derive a one-line title from the first sentence of a description."""
    text = _clean_text(description)
    if not text:
        return "Finding"
    title = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0].rstrip(" \t.,;:!?")
    if not title:
        title = text.rstrip(" \t.,;:!?") or "Finding"
    if len(title) > limit:
        cut = title[: limit - 1]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        title = cut.rstrip(" \t.,;:-") or cut
        title = f"{title}\u2026"
    return title


def _scope_line(scope: str) -> str:
    """Render the Affected Scope bullet value."""
    return f"`{_code(scope)}`" if _clean_text(scope) else "unspecified"


def _finding_block(finding: Finding) -> str:
    """Render one prioritized finding."""
    return "\n".join([
        f"### {_short_title(finding.description)} - Severity: {SEVERITY_LABEL[finding.severity]}",
        f"* **Reviewer Lens:** `{_code(finding.reviewer)}`",
        f"* **Affected Scope:** {_scope_line(finding.scope)}",
        f"* **Discrepancy Description:** {finding.description}",
        f"* **Rationale & Recommendation:** {finding.rationale or 'None provided.'}",
        f"* **Proposed Edit:** {finding.suggestion or 'None proposed'}",
    ])


def _html_escape(text: str) -> str:
    """Escape the three characters that break an HTML summary element."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_review_report(*, title: str, target: str, line_range: LineRange | None,
                         outcomes: Sequence[ReviewerOutcome], findings: Sequence[Finding],
                         mode: str = "review", body: str = "") -> str:
    """Render one review round in the upstream Goldfish report layout."""
    prepared = _prioritized(findings)
    default_title = ("Goldfish Brainstorm Brief" if str(mode).strip().lower() == "brainstorm"
                     else "Goldfish Review Report")
    blocks: list[str] = [f"# {_clean_text(title) or default_title}"]

    target_text = _clean_text(target) or "full file"
    range_text = line_range.label if line_range is not None else "full file"
    blocks.append(f"**Target:** `{_code(target_text)}` (`{range_text}`)")

    status_rows = [(f"`{_code(o.spec.name)}`", _status(o)) for o in outcomes]
    blocks.append("## Reviewer Status\n\n" + _table(["Reviewer", "Status"], status_rows))

    if prepared:
        blocks.append("## Prioritized Findings (Blockers -> Major -> Nits)")
        blocks.append("\n\n".join(_finding_block(f) for f in prepared))
    else:
        blocks.append("## Findings\n\nNo findings.")

    notes = str(body or "").strip("\n")
    if notes.strip():
        blocks.append(f"## Notes\n\n{notes}")

    details: list[str] = []
    for outcome in outcomes:
        raw = str(outcome.raw or "").strip()
        if raw:
            summary = _html_escape(_clean_text(outcome.spec.name) or "reviewer")
            details.append(f"<details><summary>{summary}</summary>\n\n{raw}\n\n</details>")
    if prepared and details:
        blocks.append("## Appendix: Raw Reviewer Reports\n" + "\n\n".join(details))

    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------- #
# Consolidated report
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Consolidated:
    """One consolidated finding plus the provenance the traceability matrix needs."""

    finding: Finding
    stage: int
    stage_title: str
    raw_ids: tuple[str, ...]
    stages: tuple[int, ...]


def _sorted_stages(stages: Sequence[StageReport]) -> list[StageReport]:
    """Order stages deterministically by number, then key and title."""
    return sorted(stages, key=lambda s: (s.stage, s.key, s.title))


def _consolidate(stages: Sequence[StageReport]) -> list[_Consolidated]:
    """Merge every stage's findings, recording which stages raised each one."""
    records: list[_Consolidated] = []
    for report in _sorted_stages(stages):
        for index, raw in enumerate(report.findings, start=1):
            candidate = _prepare(raw)
            if not candidate.description:
                continue
            key = _scope_key(candidate.scope)
            raw_id = candidate.id or f"{_id_prefix(candidate.reviewer)}-r{report.stage}-{index}"
            candidate = replace(candidate, id=raw_id)
            match = None
            for position, record in enumerate(records):
                if _same_group(record.finding, candidate, key):
                    match = position
                    break
            if match is None:
                records.append(_Consolidated(candidate, report.stage, report.title,
                                             (raw_id,), (report.stage,)))
                continue
            record = records[match]
            keep = record.finding
            severity = (candidate.severity
                        if SEVERITY_RANK.get(candidate.severity, 9)
                        < SEVERITY_RANK.get(keep.severity, 9) else keep.severity)
            rationale = _join_text(keep.rationale, candidate.rationale)
            if report.stage not in record.stages:
                rationale = _append_note(rationale, f"also raised in stage {report.stage}")
            merged = replace(
                keep,
                severity=severity,
                rationale=rationale,
                suggestion=_join_text(keep.suggestion, candidate.suggestion),
            )
            records[match] = _Consolidated(
                merged,
                record.stage,
                record.stage_title,
                record.raw_ids if raw_id in record.raw_ids else record.raw_ids + (raw_id,),
                record.stages if report.stage in record.stages else record.stages + (report.stage,),
            )
    return [record for _, record in sorted(
        enumerate(records),
        key=lambda pair: (SEVERITY_RANK.get(pair[1].finding.severity, 9), pair[0]))]


def merge_stage_findings(stages: Sequence[StageReport]) -> list[Finding]:
    """Merge stage findings, collapsing cross-stage duplicates."""
    return [record.finding for record in _consolidate(stages)]


def _consolidated_block(consolidated_id: str, record: _Consolidated) -> str:
    """Render one consolidated finding with its provenance bullets."""
    finding = record.finding
    title = _clean_text(record.stage_title)
    stage = f"{record.stage} ({title})" if title else str(record.stage)
    lines = [
        f"#### {consolidated_id} - {_short_title(finding.description)}",
        f"* **Stage:** {stage}",
        f"* **Reviewer:** `{_code(finding.reviewer)}`",
    ]
    if len(record.raw_ids) == 1:
        lines.append(f"* **Raw ID:** `{_code(record.raw_ids[0])}`")
    else:
        lines.append("* **Raw IDs:** " + ", ".join(f"`{_code(r)}`" for r in record.raw_ids))
    lines.extend([
        f"* **Affected Scope:** {_scope_line(finding.scope)}",
        f"* **Discrepancy Description:** {finding.description}",
        f"* **Rationale & Recommendation:** {finding.rationale or 'None provided.'}",
        f"* **Proposed Edit:** {finding.suggestion or 'None proposed'}",
    ])
    return "\n".join(lines)


def render_consolidated(*, target: str, split: Split | None, stages: Sequence[StageReport]) -> str:
    """Render the cross-stage consolidated report with a traceability matrix."""
    records = _consolidate(stages)

    counters = {severity: 0 for severity in SEVERITIES}
    numbered: list[tuple[str, _Consolidated]] = []
    for record in records:
        severity = record.finding.severity
        counters[severity] = counters.get(severity, 0) + 1
        numbered.append((f"{severity.upper()}-{counters[severity]:02d}", record))

    per_severity = {severity: 0 for severity in SEVERITIES}
    for record in records:
        per_severity[record.finding.severity] = per_severity.get(record.finding.severity, 0) + 1

    if split is None:
        split_line = "**Semantic split:** feature `N/A`, detailed `N/A`"
    else:
        feature = split.feature.label
        detailed = split.detailed.label if split.detailed is not None else "N/A"
        split_line = f"**Semantic split:** feature `{feature}`, detailed `{detailed}`"

    blocks: list[str] = [
        "# Consolidated Review Report",
        f"**Target:** `{_code(_clean_text(target) or 'full file')}`\n{split_line}",
    ]

    summary_rows = [(SEVERITY_LABEL[severity], str(per_severity[severity]))
                    for severity in SEVERITIES]
    summary_rows.append(("**Total**", str(len(records))))
    blocks.append("## Severity Summary\n\n" + _table(["Severity", "Count"], summary_rows))

    reviewer_rows = []
    for report in _sorted_stages(stages):
        for outcome in report.outcomes:
            reviewer_rows.append((str(report.stage), f"`{_code(outcome.spec.name)}`",
                                  _status(outcome)))
    blocks.append("## Reviewers\n\n" + _table(["Stage", "Reviewer", "Status"], reviewer_rows))

    if numbered:
        lines = ["## Consolidated Findings"]
        for severity in SEVERITIES:
            group = [(cid, record) for cid, record in numbered
                     if record.finding.severity == severity]
            if not group:
                continue
            lines.append(f"### {SEVERITY_LABEL[severity]}")
            lines.extend(_consolidated_block(cid, record) for cid, record in group)
        blocks.append("\n\n".join(lines))
    else:
        blocks.append("## Consolidated Findings\n\nNo findings.")

    matrix_rows = [(cid, ", ".join(record.raw_ids), str(record.stage),
                    _summary_label(record.finding.severity))
                   for cid, record in numbered]
    blocks.append("## Traceability Matrix\n\n"
                  + _table(["Consolidated ID", "Raw IDs", "Stage", "Severity"], matrix_rows))

    stage_rows = []
    for report in _sorted_stages(stages):
        stage_counts = counts(report.findings)
        summary = (f"{stage_counts['blocker']} blocker, {stage_counts['major']} major, "
                   f"{stage_counts['nit']} nit")
        if report.skipped:
            summary = "skipped"
        stage_rows.append((str(report.stage),
                           f"`{_code(report.path)}`" if _clean_text(report.path) else "unspecified",
                           summary))
    blocks.append("## Stage Reports\n\n" + _table(["Stage", "Report", "Counts"], stage_rows))

    return "\n\n".join(blocks) + "\n"


_FINDINGS_FENCE_RE = re.compile(r"```(?:json)?\s*\n?\s*[\[{].*?[\]}]\s*```\s*$", re.DOTALL)


def strip_finding_payload(text: str) -> str:
    """Remove a trailing machine-readable findings block, keeping the prose report.

    Stage 6's deliverable is the reviewer's paraphrase. The JSON exists so the
    engine can count and consolidate findings; it does not belong in the artifact
    a human reads.
    """
    body = str(text or "").strip()
    if not body:
        return ""
    stripped = _FINDINGS_FENCE_RE.sub("", body).rstrip()
    if stripped == body:
        # A bare JSON payload with no prose: nothing human-readable to keep.
        return "" if stripped.lstrip().startswith(("{", "[")) else stripped
    return stripped
