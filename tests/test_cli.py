from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from egm import cli

DOC = """# Feature

## Overview

Text.

## Detailed Implementation Plan

- `src/x.py` (Create)
"""


def run(argv, capsys):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    code = cli.handle(args)
    return code, capsys.readouterr().out


def test_personas_stages_and_standards(tmp_path, capsys):
    for argv in (["personas"], ["stages"], ["standards"], ["path"]):
        code, out = run(argv, capsys)
        assert code == 0
        assert out.strip()


def test_plan_reports_split(tmp_path, capsys):
    path = tmp_path / "doc.md"
    path.write_text(DOC, encoding="utf-8")
    code, out = run(["plan", str(path)], capsys)
    assert code == 0
    assert "Detailed range" in out
    assert "Stage 5" in out


def test_lint_and_json(tmp_path, capsys):
    path = tmp_path / "doc.md"
    path.write_text("# T\n\nTBD\n", encoding="utf-8")
    code, out = run(["--json", "lint", str(path)], capsys)
    assert code == 0
    assert '"findings"' in out


def test_selftest_passes(capsys):
    code, out = run(["selftest"], capsys)
    assert code == 0
    assert out.splitlines()[0] == "OK"


def test_bootstrap_and_status(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    code, out = run(["bootstrap", str(tmp_path)], capsys)
    assert code == 0
    assert "Verification:" in out
    doc = tmp_path / "d.md"
    doc.write_text(DOC, encoding="utf-8")
    code, out = run(["status", str(doc)], capsys)
    assert code == 0
    assert "Verdict:" in out


def test_missing_target_exits_two(tmp_path, capsys):
    code, _ = run(["lint", str(tmp_path / "nope.md")], capsys)
    assert code == 2
