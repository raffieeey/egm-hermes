"""Tests for :mod:`egm.drift`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import egm.drift as drift
from egm.drift import (
    DriftScan,
    Mention,
    extract_mentions,
    render_report,
    report_filename,
    scan_document,
)
from egm.types import Document


def _doc(tmp_path: Path, text: str, name: str = "DESIGN.md") -> Document:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return Document(path=path, text=text, lines=tuple(text.splitlines()), sections=())


def _write(root: Path, relative: str, content: str = "placeholder\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _never(value: str) -> bool:
    return False


def test_path_mentions_found_direct_dot_slash_and_suffix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/app/main.py")
    _write(root, "docs/guide.md")
    _write(root, "README.md")
    doc = _doc(
        tmp_path,
        "# Design\n\n"
        "Entry `src/app/main.py`, guide [docs](docs/guide.md), root `./README.md`, "
        "and suffix `app/main.py`.\n",
    )

    scan = scan_document(doc, root, search=_never)

    assert scan.missing == ()
    assert scan.verified == 4
    assert {m.value for m in scan.mentions if m.kind == "path"} == {
        "src/app/main.py", "docs/guide.md", "./README.md", "app/main.py",
    }


def test_path_mentions_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/app/main.py")
    doc = _doc(
        tmp_path,
        "Ghost `src/ghost/module.py` and [link](missing/thing.ts).\n",
    )

    scan = scan_document(doc, root, search=_never)

    assert scan.clean is False
    assert {m.value for m in scan.missing} == {"src/ghost/module.py", "missing/thing.ts"}
    assert all(m.kind == "path" for m in scan.missing)


def test_symbol_found_via_supplied_search(tmp_path: Path) -> None:
    calls: list[str] = []

    def search(value: str) -> bool:
        calls.append(value)
        return value == "WidgetFactory"

    doc = _doc(tmp_path, "The `WidgetFactory` assembles widgets.\n")

    scan = scan_document(doc, tmp_path / "repo", search=search)

    assert [m.value for m in scan.mentions if m.kind == "symbol"] == ["WidgetFactory"]
    assert scan.missing == ()
    assert scan.verified == 1
    assert calls == ["WidgetFactory"]


def test_symbol_missing(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "The `LegacyGateway.dispatch` path is described here.\n")

    scan = scan_document(doc, tmp_path / "repo", search=_never)

    assert scan.clean is False
    assert [(m.kind, m.value) for m in scan.missing] == [
        ("symbol", "LegacyGateway.dispatch"),
    ]


def test_endpoint_found_and_missing(tmp_path: Path) -> None:
    known = {"/v1/orders", "/v1/orders/{id}", "/v1/users.json"}

    def search(value: str) -> bool:
        return value in known

    doc = _doc(
        tmp_path,
        "Routes `GET /v1/orders`, `/v1/orders/{id}`, and `/v1/users.json`.\n"
        "Unknown `/v1/ghosts`.\n",
    )

    scan = scan_document(doc, tmp_path / "repo", search=search)

    endpoints = [m.value for m in scan.mentions if m.kind == "endpoint"]
    assert endpoints == ["/v1/orders", "/v1/orders/{id}", "/v1/users.json", "/v1/ghosts"]
    assert scan.verified == 3
    assert [(m.kind, m.value) for m in scan.missing] == [
        ("endpoint", "/v1/ghosts"),
    ]


def test_table_name_found_and_missing(tmp_path: Path) -> None:
    known = {"public.orders", "audit_log"}

    def search(value: str) -> bool:
        return value in known

    doc = _doc(
        tmp_path,
        "The reader loads FROM public.orders then writes INTO `audit_log`.\n"
        "Legacy rows use JOIN missing_records.\n",
    )

    scan = scan_document(doc, tmp_path / "repo", search=search)

    tables = [m.value for m in scan.mentions if m.kind == "table"]
    assert tables == ["public.orders", "audit_log", "missing_records"]
    assert scan.verified == 2
    assert [(m.kind, m.value) for m in scan.missing] == [
        ("table", "missing_records"),
    ]


def test_fenced_code_ignored_for_symbols(tmp_path: Path) -> None:
    doc = _doc(
        tmp_path,
        "# Plan\n\n"
        "```python\n"
        "WidgetFactory\n"
        "LegacyThing.run()\n"
        "```\n",
    )

    assert extract_mentions(doc) == []


def test_fenced_code_paths_are_scanned(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/legacy/worker.py")
    doc = _doc(
        tmp_path,
        "# Layout\n\n"
        "```text\n"
        "src/legacy/worker.py\n"
        "total/count\n"
        "```\n",
    )

    scan = scan_document(doc, root, search=_never)

    assert [m.value for m in scan.mentions if m.kind == "path"] == [
        "src/legacy/worker.py",
    ]
    assert scan.verified == 1
    assert scan.missing == ()


def test_keyword_exclusion(tmp_path: Path) -> None:
    doc = _doc(
        tmp_path,
        "Use `return` `if()` `class` `list` `str` `import`; "
        "the real one is `WidgetFactory`.\n",
    )

    symbols = [m.value for m in extract_mentions(doc) if m.kind == "symbol"]

    assert symbols == ["WidgetFactory"]


def test_all_caps_constants_excluded(tmp_path: Path) -> None:
    doc = _doc(
        tmp_path,
        "`MAX_RETRY_COUNT` `REQUEST_TIMEOUT` `API_KEY` are constants.\n",
    )

    assert [m for m in extract_mentions(doc) if m.kind == "symbol"] == []


def test_urls_and_globs_skipped(tmp_path: Path) -> None:
    doc = _doc(
        tmp_path,
        "See `https://example.com/app.py`, `http://x/y.md`, "
        "`src/**/*.py`, `*.md`, and `example.com/docs/x.md`.\n",
    )

    assert extract_mentions(doc) == []


def test_clean_document_reports_no_drift(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/app/main.py")
    doc = _doc(
        root,
        "# Design\n\nEntry `src/app/main.py`; builder `WidgetFactory`.\n",
    )

    scan = scan_document(doc, root, search=lambda value: value == "WidgetFactory")
    report = render_report([scan], date="2026-01-02")

    assert "- No drift detected" in report
    assert "Missing" not in report
    assert report.startswith("# Drift Report (2026-01-02)\n\n## DESIGN.md\n")


def test_skipped_only_document_reports_no_verifiable_refs(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "Call `run` when the pipeline is ready.\n")

    scan = scan_document(doc, tmp_path / "repo", search=_never)
    report = render_report([scan], date="2026-01-02")

    assert scan.clean is True
    assert [m.value for m in scan.skipped] == ["run"]
    assert scan.verified == 0
    assert "- No verifiable code references found" in report
    assert "No drift detected" not in report
    assert "Missing" not in report


def test_report_filename_format() -> None:
    assert report_filename("2026-01-02") == "2026-01-02_DRIFT_REPORT.md"
    assert report_filename(" 2026-01-02 ") == "2026-01-02_DRIFT_REPORT.md"


def test_report_ordering_and_determinism(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    alpha = _doc(root, "Missing `AlphaWidget`.\n", "a.md")
    beta = _doc(root, "Missing `BetaWidget`.\n", "b.md")

    scans = [
        scan_document(beta, root, search=_never),
        scan_document(alpha, root, search=_never),
    ]
    first = render_report(scans, date="2026-01-02")
    second = render_report(list(reversed(scans)), date="2026-01-02")

    assert first == second
    assert first.index("## a.md") < first.index("## b.md")
    assert first.endswith("- Missing symbol: `BetaWidget` (mentioned at L1)\n")
    assert "Missing symbol: `AlphaWidget`" in first


def test_default_searcher_ignores_binary_suffixes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/mod.py", "class WidgetFactory:\n    pass\n")
    _write(root, "src/binary.pyc", "WidgetFactory")
    doc = _doc(tmp_path, "# Design\n\nBuilt by `WidgetFactory`.\n")

    scan = scan_document(doc, root)

    assert scan.missing == ()
    assert scan.verified == 1


def test_default_searcher_indexes_tokens_and_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/widgets/factory.py", "class WidgetFactory:\n    pass\n")
    doc = _doc(tmp_path, "# Design\n\nBuilt by `WidgetFactory` in `src/widgets/factory.py`.\n")

    scan = scan_document(doc, root)

    assert scan.missing == ()
    assert scan.verified >= 2


def test_link_targets_and_method_pairs_are_extracted(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "src/app/main.py")
    doc = _doc(
        tmp_path,
        "Implementation [main](src/app/main.py) exposes `POST /api/v1/items`.\n",
    )

    scan = scan_document(doc, root, search=lambda value: value == "/api/v1/items")

    assert {(m.kind, m.value) for m in scan.mentions} == {
        ("path", "src/app/main.py"),
        ("endpoint", "/api/v1/items"),
    }
    assert scan.missing == ()
    assert scan.verified == 2


def test_scan_as_dict_is_json_ready(tmp_path: Path) -> None:
    doc = _doc(tmp_path, "`WidgetFactory`\n")
    scan = scan_document(doc, tmp_path, search=_never)

    payload = scan.as_dict()

    assert set(payload) == {"doc", "mentions", "missing", "skipped", "verified", "clean"}
    assert payload["missing"] == [{"kind": "symbol", "value": "WidgetFactory", "line": 1}]
    assert payload["clean"] is False
    assert Mention("symbol", "WidgetFactory", 1).as_dict() == payload["missing"][0]


def test_index_searcher_skips_dependency_directories(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "node_modules/dep/index.py", "class HiddenWidget:\n    pass\n")
    doc = _doc(tmp_path, "# Design\n\nBuilt by `HiddenWidget`.\n")

    scan = scan_document(doc, root)

    assert [(m.kind, m.value) for m in scan.missing] == [("symbol", "HiddenWidget")]


def test_drift_scan_clean_property_with_manual_construction(tmp_path: Path) -> None:
    mention = Mention("path", "src/app.py", 4)
    scan = DriftScan(tmp_path / "d.md", [mention], [], [], 1)

    assert scan.clean is True
    assert scan.mentions == (mention,)


def test_absolute_filesystem_paths_are_not_endpoints(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    doc = _doc(tmp_path, "# Design\n\nWritten to `/home/someone/project/design.md`.\n")
    scan = scan_document(doc, root, search=lambda needle: False)
    assert [(m.kind, m.value) for m in scan.missing] == []


def test_api_paths_are_still_endpoints(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    doc = _doc(tmp_path, "# Design\n\nExposes `/v1/orders/{id}`.\n")
    scan = scan_document(doc, root, search=lambda needle: False)
    assert [(m.kind, m.value) for m in scan.missing] == [("endpoint", "/v1/orders/{id}")]
