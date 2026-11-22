"""Deterministic document linting.

Fresh-context reviewers catch judgment problems; a linter catches the mechanical
ones for free and without a model call: placeholders, unclosed fences, skipped
heading levels, missing required sections, broken links, malformed tables. These
findings are merged into the same report vocabulary as the reviewers'.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import catalog, documents, reports
from .types import Document, EGMError, Finding, LineRange, severity_counts, sort_findings

_PLACEHOLDER_RE = re.compile(
    r"(?:\bTBD\b|\bTODO\b|\bFIXME\b|\bXXX\b|lorem ipsum|\[insert[^\]]*\]|<insert[^>]*>|"
    r"\{[a-z_]{3,}\}|placeholder)", re.IGNORECASE)

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "feature-spec": ("overview", "problem", "goals", "non-goals", "alternatives",
                     "risks", "technical plan"),
    "implementation-readiness": ("overview", "implementation", "test"),
    "zoom-out": ("overview",),
    "heilmeier": ("objectives", "state of the art", "new approach", "impact",
                  "risks", "cost", "timeline", "metrics"),
    "re-implementation": ("interface", "invariants", "side effects"),
}

HEILMEIER_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Objectives", "what is being attempted"),
    ("State of the Art", "how it is done today"),
    ("New Approach", "what is new"),
    ("Impact", "who cares"),
    ("Risks", "risks and the payoffs"),
    ("Cost", "how much it will cost"),
    ("Timeline", "how long it will take"),
    ("Metrics", "mid-term and final exams"),
)


def _finding(severity: str, scope: str, description: str, rationale: str = "",
             suggestion: str = "", reviewer: str = "lint") -> Finding:
    return Finding(reviewer=reviewer, severity=severity, scope=scope,
                   description=description, rationale=rationale, suggestion=suggestion)


def _fence_state(doc: Document) -> tuple[bool, int]:
    """Return (unclosed, line number of the opening fence)."""
    opened_at = 0
    marker = ""
    for index, line in enumerate(doc.lines, start=1):
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if not match:
            continue
        token = match.group(1)
        if not marker:
            marker = token[0] * 3
            opened_at = index
        elif token.startswith(marker):
            marker = ""
    return bool(marker), opened_at


def lint_document(doc: Document, standard: str = "feature-spec",
                  max_line_length: int = 0) -> list[Finding]:
    findings: list[Finding] = []
    if not standard:
        standard = "feature-spec"
    sections = list(doc.sections)

    unclosed, opened_at = _fence_state(doc)
    if unclosed:
        findings.append(_finding(
            "blocker", f"L{opened_at}",
            "Unclosed code fence.",
            "Everything after the opening fence renders as code, so later sections disappear.",
            "Close the fence or remove it."))

    for placeholder in _placeholder_matches(doc):
        findings.append(placeholder)

    for previous, current in zip(sections, sections[1:]):
        if current.level - previous.level > 1:
            findings.append(_finding(
                "major", f"L{current.start}",
                f"Heading level jumps from H{previous.level} to H{current.level}.",
                "Skipped levels break the document outline and the table of contents.",
                f"Make `{current.title}` H{previous.level + 1} or add the missing parent heading."))

    for section in sections:
        if section.level == 1:
            continue
        body = "\n".join(doc.lines[section.start:section.end]).strip()
        if not body:
            findings.append(_finding(
                "nit", section.label, f"Section `{section.title}` is empty.",
                "An empty heading promises content that is not there.",
                "Fill it in or remove the heading."))

    titles = [s.title.strip().lower() for s in sections]
    for title in sorted({t for t in titles if titles.count(t) > 1}):
        findings.append(_finding(
            "nit", title, f"Heading `{title}` appears {titles.count(title)} times.",
            "Duplicate headings make cross-references ambiguous.",
            "Disambiguate the repeated headings."))

    findings.extend(_table_findings(doc))
    findings.extend(_link_findings(doc))

    required = REQUIRED_SECTIONS.get(standard, ())
    present = " \n".join(titles)
    for name in required:
        if name not in present:
            findings.append(_finding(
                "major", "document",
                f"Required section missing for `{standard}`: {name.title()}.",
                f"The {standard} standard requires this section.",
                f"Add a `## {name.title()}` section."))

    if standard == "feature-spec":
        alternatives = doc.find_sections("alternatives")
        if not alternatives:
            findings.append(_finding(
                "blocker", "document",
                "No Alternatives Considered section.",
                "The feature-spec standard fails a proposal that never proves other viable designs were rejected.",
                "Add `## Alternatives Considered` with a concrete rejection reason for each option."))
        elif all(not "\n".join(doc.lines[s.start:s.end]).replace(s.title, "").strip()
                 for s in alternatives):
            findings.append(_finding(
                "blocker", alternatives[0].label,
                "Alternatives section is empty.",
                "The Alternatives Rule requires specific, documented rejection reasons.",
                "List each rejected option and why it lost."))

    if standard == "heilmeier":
        lowered = doc.text.lower()
        for label, question in HEILMEIER_QUESTIONS:
            if label.lower() not in lowered and question.split(" ")[0] not in lowered:
                findings.append(_finding(
                    "major", "document",
                    f"Heilmeier question unanswered: {label}.",
                    f"The catechism requires an explicit answer to {question}.",
                    f"Add a `### {label}` answer."))

    if max_line_length > 0:
        for index, line in enumerate(doc.lines, start=1):
            if len(line) > max_line_length and not line.lstrip().startswith(("http", "|")):
                findings.append(_finding(
                    "nit", f"L{index}",
                    f"Line exceeds {max_line_length} characters ({len(line)}).",
                    "Long lines are hard to review in a diff.",
                    "Wrap the line."))

    return sort_findings(findings)


def _placeholder_matches(doc: Document) -> list[Finding]:
    findings: list[Finding] = []
    for index, line in enumerate(doc.lines, start=1):
        if not _PLACEHOLDER_RE.search(line):
            continue
        if line.lstrip().startswith(("<!--", "```")):
            continue
        severity = "major" if re.search(r"\b(TBD|TODO|FIXME)\b", line, re.IGNORECASE) else "nit"
        findings.append(_finding(
            severity, f"L{index}",
            f"Placeholder left in the document: {reports.squash(line.strip(), 70)}",
            "A placeholder is an undecided design question in disguise.",
            "Resolve it or remove it."))
    return findings


def _table_findings(doc: Document) -> list[Finding]:
    findings: list[Finding] = []
    block: list[tuple[int, str]] = []

    def flush() -> None:
        if len(block) < 2:
            block.clear()
            return
        counts = {len(re.findall(r"(?<!\\)\|", line)) for _, line in block}
        if len(counts) > 1:
            findings.append(_finding(
                "nit", f"L{block[0][0]}-L{block[-1][0]}",
                "Malformed table: rows have different column counts.",
                "Markdown renders a ragged table as broken cells.",
                "Make every row use the same number of pipes."))
        block.clear()

    for index, line in enumerate(doc.lines, start=1):
        if line.strip().startswith("|"):
            block.append((index, line))
        else:
            flush()
    flush()
    return findings


def _link_findings(doc: Document) -> list[Finding]:
    findings: list[Finding] = []
    try:
        linked = documents.linked_files(doc, doc.path.parent)
    except Exception:  # noqa: BLE001
        return findings
    for path in linked.missing:
        findings.append(_finding(
            "major", "link", f"Relative link target does not exist: `{path}`.",
            "A reader who follows it hits a dead end.",
            "Fix the path or remove the link."))
    for path in linked.outside:
        findings.append(_finding(
            "nit", "link", f"Relative link escapes the document's directory: `{path}`.",
            "Links outside the working directory are not portable and need approval in a review.",
            "Move the target inside the repository or use a stable URL."))
    return findings


def run_lint(target: str | Path, standard: str = "feature-spec",
             max_line_length: int = 0) -> dict[str, object]:
    """Lint a document (or every markdown file in a directory)."""
    path = reports.resolve_target(target)
    if path.is_dir():
        files = sorted(p for p in path.rglob("*.md") if not any(
            part.startswith(".") for part in p.relative_to(path).parts))
    else:
        files = [path]
    if not files:
        raise EGMError(f"No markdown files found under {path}")
    resolved_standard = catalog.canonical_standard(standard) if standard else "feature-spec-standard"
    short_standard = resolved_standard[:-len("-standard")] if resolved_standard.endswith("-standard") else resolved_standard
    documents_out: list[dict[str, object]] = []
    all_findings: list[Finding] = []
    for file in files:
        doc = documents.read_document(file)
        findings = lint_document(doc, short_standard, max_line_length)
        all_findings.extend(findings)
        documents_out.append({"path": str(file), "counts": severity_counts(findings),
                              "findings": [f.as_dict() for f in findings]})
    return {"ok": True, "standard": short_standard, "documents": documents_out,
            "counts": severity_counts(all_findings),
            "findings": [f.as_dict() for f in sort_findings(all_findings)]}
