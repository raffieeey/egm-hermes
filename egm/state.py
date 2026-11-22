"""Round memory for the iterative review loop.

Upstream's loop driver keeps four rules that are easy to get wrong: a finding is
*settled* once contested or accepted, a *fixed* finding can be re-opened, a round
is *clean* only when no non-settled Blocker or Major remains **and** every
reviewer actually ran, and the loop *converges* only after two consecutive clean
rounds. This module is that memory, persisted per project and target.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .types import Finding, ReviewerOutcome, SEVERITIES, STATUS_COMPLETED

MAX_ROUNDS = 5
MAX_HISTORY = 12
_CLEAN_SEVERITIES = {"blocker", "major"}


def fingerprint(finding: Finding) -> str:
    """Stable identity for a finding across rounds.

    Severity is deliberately excluded: a re-run that grades the same issue as a
    Nit instead of a Major is the same issue, and must not bypass settlement.
    """
    scope = re.sub(r"\s+", " ", finding.scope.strip().lower())
    scope = re.sub(r"\d+", "#", scope)
    text = re.sub(r"[^a-z0-9 ]+", " ", finding.description.casefold())
    text = " ".join(text.split())[:220]
    digest = hashlib.sha256(f"{finding.reviewer}|{scope}|{text}".encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass
class RoundRecord:
    round: int
    clean: bool
    counts: dict[str, int]
    findings: list[dict[str, Any]] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)
    new_at_fixed_scope: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"round": self.round, "clean": self.clean, "counts": self.counts,
                "findings": self.findings, "completed": self.completed,
                "not_run": self.not_run, "new_at_fixed_scope": self.new_at_fixed_scope,
                "timestamp": self.timestamp}


@dataclass(frozen=True)
class Verdict:
    round: int
    clean: bool
    converged: bool
    cap_reached: bool
    churn: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"round": self.round, "clean": self.clean, "converged": self.converged,
                "cap_reached": self.cap_reached, "churn": self.churn, "reason": self.reason}


def state_path(project_root: Path, target: Path, *, base: Path | None = None) -> Path:
    """Where a target's loop state lives.

    Default base is the plugin data directory, so state survives plugin updates
    and never pollutes the user's repository.
    """
    if base is None:
        try:
            from plugins.plugin_storage import plugin_data_dir
            base = plugin_data_dir("egm") / "loop-state"
        except Exception:  # noqa: BLE001 - tests and CLI portability
            base = Path.home() / ".hermes" / "plugin-data" / "egm" / "loop-state"
    try:
        relative = target.resolve().relative_to(project_root.resolve())
        label = str(relative)
    except (ValueError, OSError):
        label = str(target.resolve())
    digest = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:80] or "target"
    return Path(base) / digest / f"{slug}.json"


class ReviewState:
    """Persisted review-loop state for one target."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.target: str = ""
        self.rounds: list[RoundRecord] = []
        self.settled: dict[str, dict[str, Any]] = {}
        self.fixed_scopes: list[dict[str, Any]] = []
        self.load()

    # ── persistence ───────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.target = str(data.get("target") or "")
        self.settled = {str(k): dict(v) for k, v in (data.get("settled") or {}).items()
                        if isinstance(v, dict)}
        self.fixed_scopes = [dict(v) for v in (data.get("fixed_scopes") or []) if isinstance(v, dict)]
        rounds: list[RoundRecord] = []
        for item in data.get("rounds") or []:
            if not isinstance(item, dict):
                continue
            try:
                rounds.append(RoundRecord(
                    round=int(item.get("round", len(rounds) + 1)),
                    clean=bool(item.get("clean", False)),
                    counts={str(k): int(v) for k, v in (item.get("counts") or {}).items()},
                    findings=[dict(f) for f in item.get("findings") or [] if isinstance(f, dict)],
                    completed=[str(x) for x in item.get("completed") or []],
                    not_run=[str(x) for x in item.get("not_run") or []],
                    new_at_fixed_scope=[str(x) for x in item.get("new_at_fixed_scope") or []],
                    timestamp=float(item.get("timestamp") or 0.0),
                ))
            except (TypeError, ValueError):
                continue
        self.rounds = rounds[-MAX_HISTORY:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "target": self.target,
            "updated": time.time(),
            "rounds": [r.as_dict() for r in self.rounds[-MAX_HISTORY:]],
            "settled": self.settled,
            "fixed_scopes": self.fixed_scopes[-50:],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── loop operations ───────────────────────────────────────────────────

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    def settle(self, findings: Iterable[Finding], reason: str = "contested") -> int:
        """Record findings as settled so a future reviewer cannot re-open them."""
        added = 0
        for finding in findings:
            key = fingerprint(finding)
            if key in self.settled:
                continue
            self.settled[key] = {"reviewer": finding.reviewer, "scope": finding.scope,
                                 "description": finding.description[:300], "reason": reason,
                                 "round": self.round_count}
            added += 1
        if added:
            self.save()
        return added

    def mark_fixed(self, scopes: Iterable[str]) -> None:
        """Record scopes whose fixes were applied, for churn detection."""
        existing = {entry.get("scope") for entry in self.fixed_scopes}
        for scope in scopes:
            clean = str(scope).strip()
            if clean and clean not in existing:
                self.fixed_scopes.append({"scope": clean, "round": self.round_count})
                existing.add(clean)
        self.save()

    def record(self, findings: Sequence[Finding], outcomes: Sequence[ReviewerOutcome]) -> RoundRecord:
        """Append one round and decide whether it was clean."""
        number = self.round_count + 1
        not_run = [o.spec.name for o in outcomes if o.status != STATUS_COMPLETED]
        open_findings = [f for f in findings if fingerprint(f) not in self.settled]
        blocking = [f for f in open_findings if f.severity in _CLEAN_SEVERITIES]
        clean = not blocking and not not_run
        fixed = {entry.get("scope") for entry in self.fixed_scopes}
        new_at_fixed = sorted({f.scope for f in blocking if f.scope in fixed})
        counts = {severity: 0 for severity in SEVERITIES}
        for finding in findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        record = RoundRecord(
            round=number, clean=clean, counts=counts,
            findings=[f.as_dict() for f in findings],
            completed=[o.spec.name for o in outcomes if o.status == STATUS_COMPLETED],
            not_run=not_run, new_at_fixed_scope=new_at_fixed,
        )
        self.rounds.append(record)
        self.rounds = self.rounds[-MAX_HISTORY:]
        self.save()
        return record

    def verdict(self, *, max_rounds: int = MAX_ROUNDS) -> Verdict:
        """Apply the upstream convergence rules to the recorded rounds."""
        if not self.rounds:
            return Verdict(0, False, False, False, False, "no rounds recorded")
        last = self.rounds[-1]
        clean_run = 0
        for record in reversed(self.rounds):
            if not record.clean:
                break
            clean_run += 1
        converged = clean_run >= 2
        cap = last.round >= max_rounds
        churn = bool(last.new_at_fixed_scope)
        if converged:
            reason = f"two consecutive clean rounds (round {last.round})"
        elif churn:
            reason = "churn: a new finding landed on a scope whose fix was applied"
        elif cap:
            reason = f"round cap reached ({max_rounds})"
        elif last.clean:
            reason = "one clean round; one more is required to converge"
        elif last.not_run:
            reason = f"round not clean: reviewer(s) not run: {', '.join(last.not_run)}"
        else:
            reason = "round not clean: open Blocker or Major findings remain"
        return Verdict(last.round, last.clean, converged, cap, churn, reason)

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "target": self.target,
                "rounds": [r.as_dict() for r in self.rounds],
                "settled": len(self.settled), "fixed_scopes": len(self.fixed_scopes),
                "verdict": self.verdict().as_dict()}
