"""Prompt composition for Goldfish reviewer children.

Every child gets a self-contained brief: persona, rubric, the numbered slice it
must audit, and the reading list. Nothing from the parent conversation is
forwarded. A reviewer that saw the discussion would inherit its assumptions.

The host caps a child's ``context`` at 32,000 characters, so composition is
budgeted and documents are chunked at heading boundaries rather than truncated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import catalog
from .types import Document, DocumentError, LineRange, ReviewerSpec

DEFAULT_CONTEXT_BUDGET = 26_000
HOST_CONTEXT_CAP = 32_000

_SEVERITY_GUIDE = """\
Severity levels:
- blocker: the document is wrong, unsafe, or cannot be implemented as written. A later reader inherits a false statement or a dead end.
- major: a real gap that will cost the implementer or a reviewer time: a missing decision, an unaddressed failure mode, a required section that is absent, a claim the code contradicts.
- nit: wording, formatting, or polish that does not change what gets built."""

_OUTPUT_CONTRACT = """\
## Output contract

Reply with exactly one fenced JSON block and nothing after it:

```json
{"reviewer": "%(reviewer)s", "findings": [
  {"severity": "blocker|major|nit",
   "scope": "L12-L20 or ## Section name",
   "description": "the specific gap, in one or two sentences",
   "rationale": "why it matters, citing the document or code",
   "suggestion": "the precise change, or empty string"}
]}
```

Rules for the payload:
- One object per distinct issue. Do not pad the list to look thorough.
- Cite line numbers from the numbered slice you were given. Never cite lines outside it.
- An empty `findings` array is a valid and expected answer when the slice survives your rubric.
- No prose outside the JSON block. No summary of what you read."""

_COMPREHENSION_CONTRACT = """\
## Output contract

First write the two paraphrase sections your persona requires, under exactly
these headings, 2-5 sentences each:

## What this proposes

## How the existing system works, per this document

Then write a horizontal rule (`---`) and exactly one fenced JSON block:

```json
{"reviewer": "%(reviewer)s", "findings": [
  {"severity": "blocker|major|nit",
   "scope": "L12-L20 or ## Section name",
   "description": "the specific gap, in one or two sentences",
   "rationale": "why it matters, citing the document or code",
   "suggestion": "the precise change, or empty string"}
]}
```

The paraphrase is part of the deliverable: a wrong paraphrase is itself a
finding. Cite line numbers from the numbered slice. An empty `findings` array is
expected when the document is self-contained. Nothing after the JSON block."""

_READ_ONLY = """\
## Operating rules

1. You are read-only. You are auditing a document, not fixing it.
2. You have no conversation history. Judge only what is in front of you: the slice, the rubric, and the reading list.
3. No chit-chat, no narration of tool use, no compliments. Findings only.
4. If the slice lacks the context to judge something, say so as a finding rather than guessing at intent.
5. Do not invent files, symbols, or behavior. When you check the codebase, cite what you actually found."""


@dataclass(frozen=True)
class PromptBundle:
    """One reviewer job: a goal and the context that fully determines it."""

    goal: str
    context: str

    @property
    def size(self) -> int:
        return len(self.context)


@dataclass(frozen=True)
class ContextFile:
    """A reading-list file, already bounded."""

    path: str
    text: str
    truncated: bool
    lines: int


def read_context_files(paths: list[str] | tuple[str, ...], *, root: Path | None = None,
                       budget: int = 12_000, per_file: int = 6_000) -> list[ContextFile]:
    """Read reading-list files, keeping the total bounded.

    Missing or binary files are skipped rather than failing the review: a
    reading list is advisory context, and one bad path must not stop a stage.
    """
    files: list[ContextFile] = []
    remaining = max(0, budget)
    for raw in paths:
        if remaining <= 0:
            break
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute() and root is not None:
            candidate = (root / candidate)
        try:
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
            if size > 2_000_000:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        allowance = min(per_file, remaining)
        truncated = len(text) > allowance
        chunk = text[:allowance]
        if truncated:
            chunk += f"\n… [truncated: first {allowance} of {len(text)} characters]"
        files.append(ContextFile(str(candidate), chunk, truncated, text.count("\n") + 1))
        remaining -= len(chunk)
    return files


def _document_map(doc: Document, rng: LineRange) -> str:
    lines = []
    for section in doc.sections:
        if section.start > rng.end:
            break
        if section.end < rng.start:
            continue
        lines.append(f"- {'#' * section.level} {section.title} (L{section.start}-L{section.end})")
    return "\n".join(lines) if lines else "- (no headings in this slice)"


def _context_block(files: list[ContextFile]) -> str:
    if not files:
        return ""
    parts = ["## Reading list"]
    for item in files:
        parts.append(f"### `{item.path}` ({item.lines} lines)\n```text\n{item.text}\n```")
    return "\n\n".join(parts)


def compose_review(*, spec: ReviewerSpec, persona: str, standard_text: str,
                   doc: Document, rng: LineRange, context_files: list[ContextFile],
                   mode: str = "review", extra: str = "") -> PromptBundle:
    """Build the goal and context for one reviewer over one slice."""
    reviewer = spec.name
    goal = (
        f"You are the {spec.role} Goldfish in the Elephant-Goldfish Model. "
        f"Audit `{doc.path.name}` lines {rng.start}-{rng.end} against your persona and rubric. "
        f"Return findings as JSON only.\n\n"
        f"Reviewer: {reviewer}\n"
        f"Lens: {spec.lens}\n"
        f"Target: {doc.path} ({rng.label} of {doc.line_count})\n"
        f"Standard: {spec.standard or 'none supplied — use your persona mandates'}\n"
        f"Mode: {mode}"
    )
    if len(goal) > 15_000:
        goal = goal[:15_000]

    blocks = [
        f"## Persona\n\n{persona.strip()}",
        (f"## Document standard: {spec.standard}\n\n{standard_text.strip()}"
         if standard_text else "## Document standard\n\nNo separate rubric. Apply the audit vectors in your persona."),
        (f"## Target document\n\nPath: `{doc.path}`\nSlice: {rng.label} of {doc.line_count} lines\n"
         f"Headings in this slice:\n{_document_map(doc, rng)}\n\n"
         f"```markdown\n{doc.slice(rng)}\n```"),
        _SEVERITY_GUIDE,
        (_COMPREHENSION_CONTRACT if reviewer == "comprehension" else _OUTPUT_CONTRACT)
        % {"reviewer": reviewer},
        _READ_ONLY,
    ]
    if extra:
        blocks.insert(3, f"## Stage context\n\n{extra.strip()}")
    reading = _context_block(context_files)
    if reading:
        blocks.insert(3, reading)
    context = "\n\n".join(blocks)
    return PromptBundle(goal=goal, context=context)


def compose_brainstorm(*, spec: ReviewerSpec, persona: str, hypothesis: str,
                       context_files: list[ContextFile], author: str = "") -> PromptBundle:
    """Build the goal and context for one brainstorm lens."""
    goal = (
        f"You are the {spec.role} lens in an Elephant-Goldfish brainstorm. "
        f"Challenge the feature concept below from your lens and return findings as JSON only.\n\n"
        f"Reviewer: {spec.name}\nLens: {spec.lens}\nMode: BRAINSTORM"
    )
    blocks = [
        f"## Persona\n\n{persona.strip()}",
        f"## Feature concept\n\n{hypothesis.strip()}",
        _context_block(context_files),
        _SEVERITY_GUIDE,
        _OUTPUT_CONTRACT % {"reviewer": spec.name},
        _READ_ONLY,
        ("## Brainstorm rules\n\n"
         "There is no document and no line numbers yet. Judge the concept, not a draft. "
         "Set `scope` to the aspect you are challenging (for example `scope`, `cost`, `migration`, `operations`). "
         "Every finding must name the strongest counter-argument or the missing constraint, not a restatement of the idea. "
         "If the concept survives your lens, return an empty findings array."),
    ]
    return PromptBundle(goal=goal[:15_000], context="\n\n".join(b for b in blocks if b))


def chunk_plan(doc: Document, rng: LineRange, *, spec: ReviewerSpec, persona: str,
               standard_text: str, context_files: list[ContextFile], mode: str,
               extra: str, budget: int = DEFAULT_CONTEXT_BUDGET) -> list[PromptBundle]:
    """Split a range into as many reviewer jobs as the host context cap requires.

    Composition is measured, not estimated: build the full bundle, and while it
    exceeds the budget halve the slice at a heading boundary. A single line that
    cannot fit is truncated with an explicit marker rather than silently lost.
    """
    budget = max(4_000, min(budget, HOST_CONTEXT_CAP - 1_000))
    from .documents import split_by_heading  # local import keeps module import cheap

    ranges = [rng]
    bundles: list[PromptBundle] = []
    guard = 0
    while ranges:
        guard += 1
        if guard > 400:
            raise DocumentError("Chunk planning did not converge; reduce max_context_chars.")
        current = ranges.pop(0)
        bundle = compose_review(spec=spec, persona=persona, standard_text=standard_text,
                                doc=doc, rng=current, context_files=context_files,
                                mode=mode, extra=extra)
        if bundle.size <= budget or current.length <= 1:
            if bundle.size > budget:
                bundle = PromptBundle(bundle.goal, bundle.context[:budget] +
                                      "\n… [context budget reached]")
            bundles.append(bundle)
            continue
        pieces = split_by_heading(doc, current, max_chars=max(1_000, (budget - 6_000) // 1))
        if len(pieces) <= 1:
            # No heading to cut at: halve on a line boundary.
            mid = current.start + max(1, current.length // 2) - 1
            pieces = [LineRange(current.start, mid), LineRange(mid + 1, current.end)]
        ranges = pieces + ranges
    return bundles
