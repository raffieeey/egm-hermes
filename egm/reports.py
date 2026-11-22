"""Artifact writing.

Review artifacts belong next to the document they describe (or in the
plugin-configured ``report_dir``), never in the chat transcript. This module is
the only place that decides where a report lands.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .types import DocumentError

_REPORT_NAME = re.compile(r"^[A-Za-z0-9._-]+\.md$")


def resolve_target(target: str | Path) -> Path:
    """Resolve a target path, raising a clear error when it is missing."""
    path = Path(str(target)).expanduser()
    if not path.exists():
        raise DocumentError(f"Target does not exist: {path}")
    return path.resolve()


def report_directory(target: Path, override: str = "") -> Path:
    """Pick the output directory: explicit override, else the target's own folder."""
    if override:
        directory = Path(override).expanduser()
        if not directory.is_absolute():
            directory = (target.parent / directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory.resolve()
    base = target if target.is_dir() else target.parent
    base.mkdir(parents=True, exist_ok=True)
    return base


def safe_name(name: str) -> str:
    """Reject path traversal in report filenames."""
    candidate = os.path.basename(str(name))
    if not _REPORT_NAME.match(candidate):
        raise DocumentError(f"Invalid report filename: {name!r}")
    return candidate


def write_report(directory: Path, name: str, content: str) -> Path:
    """Write a markdown artifact atomically and return its path."""
    path = Path(directory) / safe_name(name)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def display_path(path: Path) -> str:
    """Prefer a workspace-relative path for chat, fall back to absolute."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        return str(path)


def squash(text: str, limit: int = 80) -> str:
    """One-line, length-bounded label for tables and summaries."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"
