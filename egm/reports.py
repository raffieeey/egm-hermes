"""Artifact writing.

Review artifacts belong next to the document they describe (or in the
plugin-configured ``report_dir``), never in the chat transcript. This module is
the only place that decides where a report lands.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
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


@contextlib.contextmanager
def review_lock(target: Path, *, stale_seconds: int = 3600):
    """Exclusive lock per target, so two runs cannot interleave their reports.

    A tool call abandoned by the host can keep running in its thread and write
    its report later. Without a lock, a second run for the same document can
    overwrite one stage while the first is still writing another, leaving a
    consolidated report and a stage report from different runs on disk.
    """
    key = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:16]
    lock_dir = Path(tempfile.gettempdir()) / "egm-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{key}.lock"
    handle: int | None = None
    try:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            age = time.time() - lock_path.stat().st_mtime
            if age < stale_seconds:
                raise DocumentError(
                    f"Another review is already running for {target.name}. "
                    "Wait for it to finish, or remove the lock if its process died: "
                    f"{lock_path}") from None
            lock_path.unlink(missing_ok=True)
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(handle, f"{os.getpid()}\n".encode("ascii"))
        os.close(handle)
        handle = None
        yield lock_path
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)
        with contextlib.suppress(OSError):
            lock_path.unlink()
