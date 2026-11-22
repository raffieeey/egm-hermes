"""Drift detection between design documents and the code they describe.

The upstream EGM drift-report skill reads each design document, collects every
concrete reference it makes to the repository (paths, symbols, API endpoints and
table names), and reports only the references that cannot be found. This module
is the deterministic, standard-library-only half of that workflow: extraction,
verification and report rendering. It never writes files and never inspects the
clock.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence

__all__ = [
    "Mention",
    "DriftScan",
    "extract_mentions",
    "scan_document",
    "render_report",
    "report_filename",
    "repo_searcher",
]

_KINDS: tuple[str, ...] = ("path", "symbol", "endpoint", "table")

#: Source and documentation extensions accepted for extension-only paths.
_KNOWN_EXTENSIONS: frozenset[str] = frozenset({
    "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "kt", "cs", "c", "cc",
    "cpp", "h", "hpp", "md", "yaml", "yml", "json", "toml", "tf", "proto",
    "sql", "sh", "rb", "php", "swift", "dart", "lua",
})

#: Words that look like identifiers but are language keywords or type names.
_KEYWORDS: frozenset[str] = frozenset({
    "if", "else", "elif", "for", "while", "return", "true", "false", "none",
    "null", "nil", "str", "int", "bool", "list", "dict", "import", "from",
    "class", "def", "and", "or", "not", "in", "is", "as", "with", "try",
    "except", "finally", "raise", "pass", "break", "continue", "lambda",
    "yield", "async", "await", "global", "nonlocal", "assert", "del", "self",
    "cls", "void", "undefined", "var", "let", "const", "function", "new",
    "this", "super", "public", "private", "protected", "static", "final",
    "package", "interface", "enum", "struct", "type", "switch", "case",
    "default", "do", "goto", "sizeof", "namespace", "using", "template",
    "typename", "nullptr", "float", "double", "char", "byte", "object",
    # Common file extensions are never symbols.
    "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "kt", "cs", "c", "cc",
    "cpp", "h", "hpp", "md", "yaml", "yml", "json", "toml", "tf", "proto",
    "sql", "sh", "rb", "php", "swift", "dart", "lua",
    # Additional filename extensions that should never read as symbols.
    "txt", "rst", "cfg", "ini", "conf", "env", "properties", "gradle", "mod",
    "sum", "lock", "csv", "xml", "html", "htm", "css", "scss", "sass", "less",
    "svg", "png", "jpg", "jpeg", "gif", "webp", "pdf", "zip", "gz", "tgz",
    "log", "bak", "tmp", "sqlite", "db",
})

#: Lowercase words that are both everyday English and plausible symbols.
#: Unconfirmed mentions of these are reported as skipped, never as missing.
_AMBIGUOUS_SYMBOLS: frozenset[str] = frozenset({
    "run", "test", "tests", "get", "set", "add", "new", "use", "used", "end",
    "key", "name", "data", "type", "value", "values", "file", "files", "path",
    "paths", "line", "lines", "text", "node", "nodes", "item", "items", "main",
    "init", "call", "calls", "load", "save", "read", "write", "open", "close",
    "start", "stop", "make", "build", "create", "delete", "update", "check",
    "find", "parse", "render", "scan", "search", "print", "count", "error",
    "errors", "result", "results", "output", "input", "config", "default",
    "index", "root", "base", "util", "utils", "helper", "helpers", "model",
    "models", "docs", "doc", "code", "source", "target", "process", "handler",
    "client", "server", "request", "response", "event", "events", "state",
    "status", "task", "tasks", "step", "steps", "stage", "stages", "report",
    "reports", "review", "reviews", "note", "notes", "thing", "things",
    "object", "objects", "context", "session", "sessions", "user", "users",
    "group", "groups", "order", "orders", "foo", "bar", "baz", "qux", "todo",
})

#: Slash-separated phrases that are prose, not repository paths.
_SLASH_PROSE: frozenset[str] = frozenset({
    "and/or", "i/o", "w/o", "n/a", "tcp/ip", "http/https", "24/7", "yes/no",
    "on/off", "input/output", "read/write", "client/server", "key/value",
    "end/start", "he/she", "his/her", "either/or", "pros/cons", "cost/benefit",
    "risk/reward", "time/space", "push/pull", "foo/bar",
})

#: First path segments that make a slash value a reasonable repo reference.
_COMMON_TOP_DIRS: frozenset[str] = frozenset({
    "src", "docs", "doc", "tests", "test", "lib", "app", "apps", "pkg",
    "packages", "internal", "cmd", "scripts", "config", "configs", "assets",
    "examples", "samples", "benchmarks", "bench", "tools", "bin", "build",
    "dist", "public", "static", "templates", "skills", "standards", "personas",
    "migrations", "schema", "schemas", "api", "web", "server", "client",
    "frontend", "backend", "services", "core", "common", "shared", "test",
    ".github", ".hermes", ".config",
})

_BARE_URL_TLDS: frozenset[str] = frozenset({
    "com", "org", "net", "io", "dev", "ai", "co", "app", "edu", "gov", "info",
    "biz", "xyz", "me", "sh", "cloud", "site", "tech", "page", "blog",
})

_SQL_KEYWORDS: frozenset[str] = frozenset({
    "select", "where", "on", "set", "values", "insert", "delete", "create",
    "table", "index", "view", "schema", "database", "primary", "foreign",
    "constraint", "references", "join", "inner", "left", "right", "outer",
    "full", "cross", "natural", "using", "group", "order", "by", "having",
    "limit", "offset", "union", "all", "distinct", "as", "and", "or", "not",
    "null", "true", "false", "case", "when", "then", "else", "end", "begin",
    "commit", "rollback", "transaction", "returning", "with", "recursive",
    "lateral", "dual", "into", "from", "update", "exists", "between", "like",
    "is", "in", "any", "some", "asc", "desc", "over", "partition", "window",
})

#: Everyday words that are not table names when they follow FROM/JOIN/INTO/UPDATE.
_ENGLISH_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "each", "every", "any",
    "all", "both", "either", "neither", "no", "not", "only", "also", "our",
    "your", "their", "its", "his", "her", "my", "we", "you", "they", "i", "he",
    "she", "it", "them", "us", "him", "is", "are", "was", "were", "be", "been",
    "being", "has", "have", "had", "do", "does", "did", "can", "could", "may",
    "might", "must", "shall", "should", "will", "would", "of", "to", "in", "on",
    "at", "by", "for", "with", "without", "into", "onto", "over", "under",
    "between", "among", "through", "during", "before", "after", "above",
    "below", "and", "or", "but", "if", "then", "than", "so", "such", "as",
    "because", "while", "when", "where", "which", "who", "whom", "whose",
    "what", "how", "why", "new", "old", "current", "existing", "previous",
    "next", "last", "first", "second", "one", "two", "three", "source",
    "target", "value", "values", "name", "names", "type", "types", "kind",
    "kinds", "item", "items", "thing", "things", "code", "file", "files",
    "path", "paths", "line", "lines", "text", "data", "result", "results",
    "output", "input", "table", "tables", "column", "columns", "row", "rows",
    "field", "fields", "record", "records", "use", "used", "using", "make",
    "makes", "made", "get", "gets", "set", "sets", "add", "adds", "update",
    "updates", "delete", "deletes", "insert", "note", "notes", "example",
    "examples", "see", "docs", "documentation", "section", "sections", "step",
    "steps", "this", "it", "there", "here",
})

#: Directories never walked when building the suffix index or fallback search.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "dist", "build",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
})

_BINARY_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov",
    ".avi", ".mkv", ".wav", ".ogg", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".pyc", ".pyo", ".class", ".jar", ".wasm", ".sqlite", ".db", ".lock",
    ".DS_Store",
})

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_INDEX_FILES = 20000

_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"(?<![`])(`+)(?![`])(.+?)(?<![`])\1(?![`])")
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)")
_LINK_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
_IDENTIFIER_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?"
)
_ALL_CAPS_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_METHOD_PATH_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"
    r"(/[^\s`\"'<>()\[\]|,;]+)",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(
    r"\b(FROM|JOIN|INTO|UPDATE)\s+[`\"\[]?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_BOX_CHARS = "│├└─┬┴┼━┃┏┓┗┛┣┫┳┻╋┌┐┘┖"
_TRIM_CHARS = " \t\r\n`\"'()[]<>,;:!?*|"
_FORBIDDEN_PATH_CHARS = set('*?[]{}<>|$`"\\@=,:;')


@dataclass(frozen=True)
class Mention:
    """One concrete repository reference found in a design document."""

    kind: str
    value: str
    line: int

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown mention kind {self.kind!r}")
        if not self.value:
            raise ValueError("mention value must be non-empty")
        if self.line < 1:
            raise ValueError("mention line must be 1-based")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation."""
        return {"kind": self.kind, "value": self.value, "line": self.line}


@dataclass(frozen=True)
class DriftScan:
    """The mentions extracted from one document and their verification outcome."""

    doc: Path
    mentions: tuple[Mention, ...]
    missing: tuple[Mention, ...]
    skipped: tuple[Mention, ...]
    verified: int
    root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "doc", Path(self.doc))
        for name in ("mentions", "missing", "skipped"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))
        if self.root is not None:
            object.__setattr__(self, "root", Path(self.root))

    @property
    def clean(self) -> bool:
        """True when the document produced no missing references."""
        return not self.missing

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation."""
        return {
            "doc": str(self.doc),
            "mentions": [m.as_dict() for m in self.mentions],
            "missing": [m.as_dict() for m in self.missing],
            "skipped": [m.as_dict() for m in self.skipped],
            "verified": self.verified,
            "clean": self.clean,
        }


def extract_mentions(doc: Document) -> list[Mention]:
    """Extract path, symbol, endpoint and table mentions from a parsed document."""
    found: list[Mention] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, line: int) -> None:
        key = (kind, value)
        if not value or key in seen:
            return
        seen.add(key)
        found.append(Mention(kind, value, line))

    for lineno, line, in_fence in _iter_lines(doc.lines):
        if in_fence:
            if _is_preprocessor_include(line):
                continue
            for token in _fenced_tokens(line):
                value = _clean_path(token)
                if _looks_like_path(value, in_fence=True):
                    add("path", value, lineno)
            continue

        spans = _inline_code_spans(line)
        prose = _blank_spans(line, spans)
        table_matches = [
            match for match in _TABLE_REF_RE.finditer(line)
            if not _is_python_import_tail(line, match.end())
            and line[match.end():match.end() + 1] not in {"/", "\\"}
        ]
        table_values = {
            _clean_table(match.group(2)) for match in table_matches
        }
        table_values.discard("")

        for _start, _end, raw in spans:
            text = raw.strip()
            if not text:
                continue
            for match in _METHOD_PATH_RE.finditer(text):
                endpoint = _clean_path(match.group(1))
                if _looks_like_endpoint(endpoint):
                    add("endpoint", endpoint, lineno)
            candidate = _clean_path(text)
            if _looks_like_endpoint(candidate):
                add("endpoint", candidate, lineno)
            elif _looks_like_path(candidate):
                add("path", candidate, lineno)
            symbol = _clean_symbol(text)
            if symbol and symbol not in table_values:
                add("symbol", symbol, lineno)

        for target in _link_targets(prose):
            value = _clean_path(target)
            if _looks_like_path(value):
                add("path", value, lineno)

        for target in _link_definitions(line):
            value = _clean_path(target)
            if _looks_like_path(value):
                add("path", value, lineno)

        for match in _METHOD_PATH_RE.finditer(prose):
            endpoint = _clean_path(match.group(1))
            if _looks_like_endpoint(endpoint):
                add("endpoint", endpoint, lineno)

        for match in table_matches:
            table = _clean_table(match.group(2))
            if table:
                add("table", table, lineno)

    return found


def scan_document(
    doc: Document,
    root: str | Path,
    *,
    search: Callable[[str], bool] | None = None,
) -> DriftScan:
    """Verify every extracted mention against ``root`` and classify the result."""
    root_path = Path(root)
    mentions = extract_mentions(doc)
    searcher = search if search is not None else _default_searcher(root_path)
    index = _RepoIndex(root_path)

    missing: list[Mention] = []
    skipped: list[Mention] = []
    verified = 0

    for mention in mentions:
        if mention.kind == "path":
            ok = _verify_path(root_path, mention.value, index)
        else:
            ok = _run_search(searcher, mention.value)
            if not ok and mention.kind == "symbol" and mention.value.endswith("()"):
                ok = _run_search(searcher, mention.value[:-2])
            if not ok and mention.kind == "symbol" and _is_ambiguous_symbol(mention.value):
                skipped.append(mention)
                continue
        if ok:
            verified += 1
        else:
            missing.append(mention)

    return DriftScan(
        doc=doc.path,
        mentions=tuple(mentions),
        missing=tuple(missing),
        skipped=tuple(skipped),
        verified=verified,
        root=root_path,
    )


def render_report(scans: Sequence[DriftScan], *, date: str) -> str:
    """Render the upstream drift-report layout for the given scans."""
    stamp = str(date).strip()
    if not stamp:
        raise ValueError("date is required")
    lines = [f"# Drift Report ({stamp})", ""]

    ordered = sorted(scans, key=lambda scan: (_display_path(scan), str(scan.doc)))
    for scan in ordered:
        lines.append(f"## {_display_path(scan)}")
        lines.append("")
        if scan.missing:
            kind_order = {kind: i for i, kind in enumerate(_KINDS)}
            for mention in sorted(
                scan.missing,
                key=lambda m: (m.line, kind_order.get(m.kind, len(_KINDS)), m.value),
            ):
                lines.append(
                    f"- Missing {mention.kind}: `{mention.value}` "
                    f"(mentioned at L{mention.line})"
                )
        elif scan.verified:
            lines.append("- No drift detected")
        elif scan.skipped:
            lines.append("- No verifiable code references found")
        else:
            lines.append("- No drift detected")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def report_filename(date: str) -> str:
    """Return the dated upstream report filename."""
    stamp = str(date).strip()
    if not stamp:
        raise ValueError("date is required")
    return f"{stamp}_DRIFT_REPORT.md"


# ---------------------------------------------------------------------------
# Markdown scanning helpers
# ---------------------------------------------------------------------------


def _iter_lines(lines: Sequence[str]) -> Iterator[tuple[int, str, bool]]:
    """Yield ``(lineno, line, in_fence)``, tracking fenced code blocks."""
    fence_char = ""
    fence_len = 0
    in_fence = False
    for lineno, line in enumerate(lines, 1):
        match = _FENCE_RE.match(line)
        if in_fence:
            if (
                match
                and match.group(1)[0] == fence_char
                and len(match.group(1)) >= fence_len
                and not match.group(2).strip()
            ):
                in_fence = False
            yield lineno, line, True
            continue
        if match:
            in_fence = True
            fence_char = match.group(1)[0]
            fence_len = len(match.group(1))
            yield lineno, line, True
            continue
        yield lineno, line, False


def _inline_code_spans(line: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, content)`` for every inline code span on a line."""
    return [(m.start(), m.end(), m.group(2)) for m in _INLINE_CODE_RE.finditer(line)]


def _blank_spans(line: str, spans: Sequence[tuple[int, int, str]]) -> str:
    """Replace inline code spans with spaces, preserving offsets."""
    chars = list(line)
    for start, end, _content in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _link_targets(prose: str) -> list[str]:
    """Return inline markdown link targets from a line with code spans blanked."""
    targets: list[str] = []
    for match in _LINK_RE.finditer(prose):
        target = match.group(1).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if target:
            targets.append(target)
    return targets


def _link_definitions(line: str) -> list[str]:
    """Return reference-style link definition targets from a line."""
    match = _LINK_DEF_RE.match(line)
    if not match:
        return []
    target = match.group(1).strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    return [target] if target else []


def _is_preprocessor_include(line: str) -> bool:
    """True for a C/C++ ``#include`` line, whose header names are not repo paths."""
    return bool(re.match(r"^\s*#\s*include\b", line))


def _fenced_tokens(line: str) -> list[str]:
    """Split a fenced-code line into path-shaped token candidates."""
    tokens: list[str] = []
    for raw in re.split(r"\s+", line):
        token = raw.strip().strip(_BOX_CHARS)
        token = token.strip(_TRIM_CHARS)
        token = token.strip(_BOX_CHARS)
        if token:
            tokens.append(token)
    return tokens


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _clean_token(text: str) -> str:
    """Trim markdown/command punctuation from a candidate token."""
    value = text.strip()
    value = value.strip("`")
    value = value.strip(_TRIM_CHARS)
    value = value.strip(_BOX_CHARS)
    value = value.strip(_TRIM_CHARS)
    while value.endswith("}") and value.count("}") > value.count("{"):
        value = value[:-1]
    return value.strip()


def _clean_path(raw: str) -> str:
    """Normalize a path or endpoint candidate: strip anchor, query and line suffix."""
    value = _clean_token(raw)
    if not value:
        return ""
    value = value.split("#", 1)[0]
    value = value.split("?", 1)[0]
    value = _LINE_SUFFIX_RE.sub("", value)
    value = value.rstrip(".,;:")
    if value.endswith("/") and len(value) > 1:
        value = value.rstrip("/")
    return value.strip()


def _extension(value: str) -> str:
    """Return the lowercase final extension of a path-like value."""
    name = value.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _looks_like_path(value: str, *, in_fence: bool = False) -> bool:
    """Decide whether a token is a repository path worth verifying.

    ``in_fence`` applies a stricter rule for code blocks, where arithmetic such
    as ``total/count`` otherwise looks like a path.
    """
    if not value or len(value) < 2 or len(value) > 300:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if any(ord(ch) < 32 for ch in value):
        return False
    if "://" in value:
        return False
    lowered = value.lower()
    if lowered.startswith(("http:", "https:", "mailto:", "ftp:", "ssh:", "tel:",
                           "data:", "file:", "git@", "www.")):
        return False
    if value.startswith(("#", "/", "-", "~")):
        return False
    if value in {".", "..", "./", "../"}:
        return False
    if any(ch in _FORBIDDEN_PATH_CHARS for ch in value):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)*", value) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    if lowered in _SLASH_PROSE:
        return False
    if _looks_like_bare_url(value):
        return False

    ext = _extension(value)
    has_ext = ext in _KNOWN_EXTENSIONS and len(value) > len(ext) + 1
    has_slash = "/" in value
    if not has_slash:
        return has_ext
    if value[0].isdigit() and not has_ext:
        return False

    segments = [segment for segment in value.split("/") if segment]
    if len(segments) < 2:
        return False
    if all(len(segment) == 1 and segment.isalpha() for segment in segments):
        return False
    if in_fence and not has_ext:
        explicit = value.startswith(("./", "../"))
        directoryish = any(
            "_" in segment or "-" in segment or "." in segment
            for segment in segments
        )
        common_top = segments[0].lower() in _COMMON_TOP_DIRS
        deep = value.count("/") >= 2
        if not (explicit or directoryish or common_top or deep):
            return False
    return True


def _looks_like_bare_url(value: str) -> bool:
    """True for scheme-less URLs such as ``github.com/org/repo``."""
    if "/" not in value:
        return False
    first = value.split("/", 1)[0].lower()
    if first.startswith("www."):
        return True
    if "." not in first:
        return False
    return first.rsplit(".", 1)[-1] in _BARE_URL_TLDS


_FILESYSTEM_ROOTS = frozenset({
    "home", "users", "tmp", "var", "usr", "etc", "opt", "root", "mnt", "media",
    "srv", "proc", "dev", "sys", "bin", "sbin", "library", "applications", "volumes",
})


def _looks_like_endpoint(value: str) -> bool:
    """True for an API path such as ``/v1/orders`` or ``/v1/orders/{id}``.

    Absolute filesystem paths also start with ``/``. They are not endpoints, and
    reporting one as a missing endpoint is exactly the false drift claim this
    module exists to avoid, so reject filesystem roots and file extensions.
    """
    if not value.startswith("/") or len(value) < 4 or len(value) > 120:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if "://" in value or value.startswith("//"):
        return False
    segments = [segment for segment in value.split("/") if segment]
    if len(segments) < 2:
        return False
    if segments[0].lower() in _FILESYSTEM_ROOTS:
        return False
    for segment in segments:
        if not re.fullmatch(r"[A-Za-z0-9._~%{}+-]+", segment):
            return False
    return True


def _clean_symbol(text: str) -> str:
    """Return an identifier-shaped inline-code value, or ``""`` when it is not one."""
    value = text.strip().strip("`").strip()
    if not value or any(ch.isspace() for ch in value):
        return ""
    if not _IDENTIFIER_RE.fullmatch(value):
        return ""
    base = value[:-2] if value.endswith("()") else value
    if len(base) < 3:
        return ""
    if base.lower() in _KEYWORDS:
        return ""
    if _ALL_CAPS_RE.fullmatch(base):
        return ""
    if base.lower().startswith("www."):
        return ""
    parts = base.split(".")
    if len(parts) > 1 and parts[-1].lower() in _BARE_URL_TLDS:
        # ``example.com`` is a host, not a qualified symbol.
        return ""
    if _looks_like_path(value):
        return ""
    return value


def _clean_table(raw: str) -> str:
    """Return a plausible SQL table identifier, or ``""`` when it is a keyword/word."""
    value = raw.strip().strip("`\"'[]").rstrip(".,;:)")
    if not value:
        return ""
    parts = value.split(".")
    if any(not part for part in parts):
        return ""
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        return ""
    last = parts[-1].lower()
    if last in _SQL_KEYWORDS or last in _ENGLISH_STOPWORDS or last in _KEYWORDS:
        return ""
    if len(last) < 3:
        return ""
    if any(part.lower() in _SQL_KEYWORDS for part in parts[:-1]):
        return ""
    return value


def _is_python_import_tail(line: str, end: int) -> bool:
    """True when a FROM match is really a Python ``from x import y`` statement."""
    tail = line[end:end + 24]
    return bool(re.match(r"\s+import\b", tail))


def _is_ambiguous_symbol(value: str) -> bool:
    """True for symbols that double as everyday words and need confirmation."""
    base = value[:-2] if value.endswith("()") else value
    lowered = base.lower()
    if lowered in _AMBIGUOUS_SYMBOLS:
        return True
    if "." not in base and base.islower() and len(base) <= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def _run_search(searcher: Callable[[str], bool], value: str) -> bool:
    """Call a searcher, treating any failure as an unconfirmed mention."""
    try:
        return bool(searcher(value))
    except Exception:  # noqa: BLE001 - an unverifiable mention is skipped, not missing
        return False


class _RepoIndex:
    """A lazily built, bounded list of root-relative POSIX file paths."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        """Return (and cache) root-relative POSIX paths, bounded by count."""
        if self._files is not None:
            return self._files
        collected: list[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                    collected.append(rel)
                    if len(collected) >= _MAX_INDEX_FILES:
                        self._files = collected
                        return self._files
        except OSError:
            pass
        self._files = collected
        return self._files


def _verify_path(root: Path, value: str, index: _RepoIndex) -> bool:
    """Verify a path directly, with ``./``, then by repository suffix."""
    rel = value.replace("\\", "/")
    for candidate in (os.path.join(str(root), rel),
                      os.path.join(str(root), ".", rel)):
        try:
            if os.path.exists(candidate):
                return True
        except OSError:
            pass

    normalized = rel
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    if not normalized:
        return False
    needle = "/" + normalized
    try:
        for repo_file in index.files():
            if repo_file == normalized or repo_file.endswith(needle):
                return True
    except OSError:
        return False
    return False


def _default_searcher(root: Path) -> Callable[[str], bool]:
    """Build a literal searcher from one in-process pass over the repository.

    One walk builds a token index (identifiers, dotted names and file paths), so
    verifying fifty mentions costs one read of the tree, not fifty. No
    subprocess, no shell, no network.
    """
    tokens: set[str] = set()
    paths: list[str] = []
    root_text = str(root)
    visited = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root_text, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                visited += 1
                if visited > _MAX_INDEX_FILES:
                    break
                full = os.path.join(dirpath, name)
                relative = os.path.relpath(full, root_text).replace(os.sep, "/")
                paths.append(relative)
                if _skip_binary_file(name):
                    continue
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                    with open(full, "rb") as handle:
                        raw = handle.read(_MAX_FILE_BYTES + 1)
                except OSError:
                    continue
                if len(raw) > _MAX_FILE_BYTES:
                    continue
                tokens.update(_TOKEN_RE.findall(raw.decode("utf-8", "ignore")))
            if visited > _MAX_INDEX_FILES:
                break
    except OSError:
        pass
    path_blob = "\n".join(paths)

    def searcher(needle: str) -> bool:
        text = str(needle or "").strip()
        if not text:
            return False
        if text in tokens:
            return True
        bare = text.rstrip("()")
        last = bare.split(".")[-1]
        if last and last in tokens:
            return True
        return text in path_blob or bare in path_blob

    return searcher


def repo_searcher(root: str | Path) -> Callable[[str], bool]:
    """One-pass literal searcher for a repository, reusable across documents."""
    return _default_searcher(Path(root))


def _skip_binary_file(name: str) -> bool:
    """True when a filename has a known binary suffix."""
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in _BINARY_SUFFIXES)


def _display_path(scan: DriftScan) -> str:
    """Render a document path relative to the scan root when possible."""
    doc = Path(scan.doc)
    if scan.root is not None:
        try:
            doc_abs = os.path.abspath(str(doc))
            root_abs = os.path.abspath(str(scan.root))
            rel = os.path.relpath(doc_abs, root_abs)
            if rel != os.curdir:
                return Path(rel).as_posix()
        except (OSError, ValueError):
            pass
    return doc.as_posix()
