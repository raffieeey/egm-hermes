"""Document intake: parsing, split discovery, linked files and chunking.

The rest of the plugin only ever sees :class:`~egm.types.Document`, so all
markdown knowledge lives here: fenced-code-aware heading extraction, the
feature/implementation boundary search, the linked-file safety gate and the
heading-aligned chunker used to stay inside a character budget.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .types import Document, DocumentError, LineRange, Section, Split

__all__ = [
    "LinkedFiles",
    "discover_split",
    "linked_files",
    "parse_document",
    "read_document",
    "split_by_heading",
]

#: Heading spellings the upstream review protocol accepts as the primary divider.
_PLAN_HEADINGS: tuple[str, ...] = (
    "detailed implementation plan",
    "implementation plan",
    "work breakdown",
    "implementation steps",
)

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*$")
_INLINE_LINK_RE = re.compile(r"\[[^\]\n]*\]\(")
_AUTOLINK_RE = re.compile(r"<([^<>\s]+)>")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_BARE_TAG_RE = re.compile(r"/?[A-Za-z][A-Za-z0-9-]*/?")


@dataclass(frozen=True)
class LinkedFiles:
    """Linked files grouped by whether they are safe to read as context."""

    included: tuple[Path, ...] = ()
    outside: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        """Return the four groups as JSON-ready lists of strings."""
        return {
            "included": [str(path) for path in self.included],
            "outside": list(self.outside),
            "missing": list(self.missing),
            "urls": list(self.urls),
        }


def read_document(path: str | Path) -> Document:
    """Read a UTF-8 markdown file from disk and parse it."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise DocumentError(f"Cannot read document {target}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentError(f"Document {target} is not valid UTF-8: {exc}") from exc
    return parse_document(text, target)


def parse_document(text: str, path: str | Path = "<memory>") -> Document:
    """Parse markdown text into a Document whose sections ignore fenced code."""
    lines = tuple(text.splitlines())
    fenced = _fenced_mask(lines)
    headings: list[tuple[int, int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        if fenced[number - 1]:
            continue
        parsed = _parse_heading(line)
        if parsed is not None:
            level, title = parsed
            headings.append((number, level, title, line))

    sections: list[Section] = []
    for index, (start, level, title, raw) in enumerate(headings):
        end = len(lines) if lines else start
        for later_start, later_level, _, _ in headings[index + 1:]:
            if later_level <= level:
                end = later_start - 1
                break
        sections.append(Section(level=level, title=title, start=start, end=end, raw=raw))

    return Document(path=Path(path), text=text, lines=lines, sections=tuple(sections))


def discover_split(doc: Document) -> Split:
    """Locate the feature/implementation boundary heading, if it is unambiguous."""
    total = max(1, doc.line_count)
    candidates = [section for section in doc.sections if _is_plan_heading(section.title)]

    if not candidates:
        return Split(feature=LineRange(1, total), detailed=None)

    if len(candidates) > 1:
        first = candidates[0]
        return Split(
            feature=LineRange(1, total),
            detailed=None,
            boundary_line=first.start,
            boundary_title=first.title,
            ambiguous=tuple(candidates),
        )

    boundary = candidates[0]
    if boundary.start <= 1:
        feature = LineRange(1, 1)
        detailed: LineRange | None = LineRange(1, total)
    else:
        feature = LineRange(1, boundary.start - 1)
        detailed = LineRange(boundary.start, total)
    return Split(
        feature=feature,
        detailed=detailed,
        boundary_line=boundary.start,
        boundary_title=boundary.title,
    )


def linked_files(doc: Document, root: str | Path) -> LinkedFiles:
    """Classify markdown links relative to root as included, outside, missing or URLs."""
    root_real = Path(os.path.realpath(os.fspath(root)))
    fenced = _fenced_mask(doc.lines)
    base = doc.path.parent

    included: list[Path] = []
    outside: list[str] = []
    missing: list[str] = []
    urls: list[str] = []
    seen: set[str] = set()

    for line, is_fenced in zip(doc.lines, fenced):
        if is_fenced:
            continue
        for raw in _targets_in_line(line):
            target = _clean_target(raw)
            if not target:
                continue

            if _SCHEME_RE.match(target):
                key = f"url:{target}"
                if key not in seen:
                    seen.add(key)
                    urls.append(target)
                continue

            local = unquote(target)
            candidate = Path(local)
            if not candidate.is_absolute():
                candidate = base / candidate
            candidate = Path(os.path.abspath(os.fspath(candidate)))
            resolved = Path(os.path.realpath(os.fspath(candidate)))
            key = f"path:{resolved}"
            if key in seen:
                continue
            seen.add(key)

            if resolved != root_real and root_real not in resolved.parents:
                outside.append(local)
            elif not candidate.exists():
                missing.append(local)
            else:
                included.append(resolved)

    return LinkedFiles(
        included=tuple(included),
        outside=tuple(outside),
        missing=tuple(missing),
        urls=tuple(urls),
    )


def split_by_heading(doc: Document, rng: LineRange, *, max_chars: int) -> list[LineRange]:
    """Cut a line range into contiguous chunks that fit the character budget."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not doc.lines:
        return [LineRange(1, 1)]

    window = rng.clamp(doc.line_count)
    heading_starts = sorted(
        {section.start for section in doc.sections
         if window.start <= section.start <= window.end}
    )

    chunks: list[LineRange] = []
    start = window.start
    while start <= window.end:
        consumed = 0
        furthest = start - 1
        for number in range(start, window.end + 1):
            addition = len(doc.lines[number - 1]) + (1 if number > start else 0)
            if number > start and consumed + addition > max_chars:
                break
            consumed += addition
            furthest = number
        if furthest < start:
            furthest = start

        cuts = [heading for heading in heading_starts if start < heading <= furthest]
        end = cuts[-1] - 1 if cuts else furthest
        chunks.append(LineRange(start, end))
        start = end + 1

    return chunks


def _normalize_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title).strip().rstrip(":").strip()
    return text.casefold()


def _is_plan_heading(title: str) -> bool:
    normalized = _normalize_title(title)
    for synonym in _PLAN_HEADINGS:
        if normalized == synonym:
            return True
        if normalized.startswith(synonym):
            remainder = normalized[len(synonym):]
            if remainder and not remainder[0].isalnum():
                return True
    return False


def _parse_heading(line: str) -> tuple[int, str] | None:
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    level = len(match.group(1))
    title = (match.group(2) or "").strip()
    title = re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()
    return level, title


def _opening_fence(line: str) -> tuple[str, int] | None:
    match = _FENCE_OPEN_RE.match(line)
    if match is None:
        return None
    marker = match.group(1)[0]
    if marker == "`" and "`" in match.group(2):
        return None
    return marker, len(match.group(1))


def _closing_fence(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return None
    match = _FENCE_CLOSE_RE.match(stripped)
    if match is None:
        return None
    return match.group(1)[0], len(match.group(1))


def _fenced_mask(lines: tuple[str, ...]) -> list[bool]:
    mask = [False] * len(lines)
    marker = ""
    size = 0
    for index, line in enumerate(lines):
        if marker:
            mask[index] = True
            closer = _closing_fence(line)
            if closer is not None and closer[0] == marker and closer[1] >= size:
                marker, size = "", 0
            continue
        opener = _opening_fence(line)
        if opener is not None:
            marker, size = opener
            mask[index] = True
    return mask


def _balanced_destination(line: str, start: int) -> str | None:
    depth = 1
    index = start
    while index < len(line):
        char = line[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return line[start:index]
        index += 1
    return None


def _strip_link_title(destination: str) -> str:
    text = destination.strip()
    if text.startswith("<"):
        close = text.find(">")
        if close != -1:
            return text[1:close]
    titled = re.match(r"^(.*?)\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\))\s*$", text, re.S)
    if titled is not None:
        return titled.group(1).strip()
    return text


def _clean_target(destination: str) -> str:
    text = _strip_link_title(destination).strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    text = text.split("#", 1)[0].strip()
    return text


def _is_autolink_target(body: str) -> bool:
    if not body or any(char.isspace() for char in body):
        return False
    if _BARE_TAG_RE.fullmatch(body):
        return False
    return _SCHEME_RE.match(body) is not None or "." in body or "/" in body


def _targets_in_line(line: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for match in _INLINE_LINK_RE.finditer(line):
        destination = _balanced_destination(line, match.end())
        if destination is not None:
            found.append((match.start(), destination))
    for match in _AUTOLINK_RE.finditer(line):
        if _is_autolink_target(match.group(1)):
            found.append((match.start(), match.group(1)))
    found.sort(key=lambda item: item[0])
    return [value for _, value in found]
