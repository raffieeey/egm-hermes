"""Concurrent Goldfish fan-out over the public subagent lifecycle API.

One job is (reviewer, slice). A job becomes one or more children when the slice
does not fit the host's 32k context cap. Children are launched together under a
concurrency cap, waited on independently, retried once on failure, and reported
as ``completed`` or ``not run`` — never as a reviewer with zero findings when it
did not actually review.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .prompts import PromptBundle
from .synth import parse_findings
from .types import (EGMError, Finding, ReviewerOutcome, ReviewerSpec,
                    STATUS_COMPLETED, STATUS_NOT_RUN)

EventHook = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class SwarmConfig:
    """Tunables read from ``plugins.entries.egm.settings``."""

    max_reviewers: int = 6
    retries: int = 1
    timeout: float = 600.0
    dual_run: bool = False
    model: str = ""
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"max_reviewers": self.max_reviewers, "retries": self.retries,
                "timeout": self.timeout, "dual_run": self.dual_run,
                "model": self.model, "provider": self.provider}


@dataclass(frozen=True)
class ReviewJob:
    """A reviewer and the one or more slices it must audit."""

    spec: ReviewerSpec
    bundles: tuple[PromptBundle, ...]

    @property
    def chunks(self) -> int:
        return len(self.bundles)


class SwarmError(EGMError):
    """No child could be launched, or the host refused the launch."""


class SwarmRunner:
    """Launches reviewer children and normalizes their reports."""

    def __init__(self, ctx: Any, config: SwarmConfig | None = None,
                 on_event: EventHook | None = None) -> None:
        self._ctx = ctx
        self.config = config or SwarmConfig()
        self._on_event = on_event
        self._lock = threading.Lock()
        self._sequence = 0
        self._run_token = f"{int(time.time() * 1000):x}"

    # ── public API ────────────────────────────────────────────────────────

    def run(self, jobs: Sequence[ReviewJob]) -> list[ReviewerOutcome]:
        if not jobs:
            return []
        units: list[tuple[int, int, ReviewJob, PromptBundle]] = []
        for job_index, job in enumerate(jobs):
            repeats = 2 if self.config.dual_run else 1
            for repeat in range(repeats):
                for chunk_index, bundle in enumerate(job.bundles):
                    units.append((job_index, chunk_index + repeat * 1000, job, bundle))
        workers = max(1, min(self.config.max_reviewers, len(units)))
        self._emit("swarm.start", {"jobs": len(jobs), "children": len(units), "workers": workers})
        collected: dict[int, list[tuple[Finding, str]]] = {i: [] for i in range(len(jobs))}
        failures: dict[int, list[str]] = {i: [] for i in range(len(jobs))}
        raws: dict[int, list[str]] = {i: [] for i in range(len(jobs))}
        attempts: dict[int, int] = {i: 1 for i in range(len(jobs))}
        started = time.time()
        # The lifecycle service resolves the parent agent from a contextvar set by
        # the current turn. Worker threads start with an empty context, so capture
        # the caller's context here and enter a copy in each thread; without this
        # every launch fails with "No active Hermes parent session".
        base_context = contextvars.copy_context()

        def work(unit: tuple[int, int, ReviewJob, PromptBundle]) -> None:
            job_index, chunk_index, job, bundle = unit
            try:
                text, used = base_context.copy().run(self._run_unit, job, bundle, chunk_index)
            except Exception as exc:  # noqa: BLE001 - every failure is a "not run", not a crash
                with self._lock:
                    failures[job_index].append(_short_error(exc))
                self._emit("reviewer.failed", {"reviewer": job.spec.name,
                                               "chunk": chunk_index, "error": _short_error(exc)})
                return
            with self._lock:
                raws[job_index].append(text)
                attempts[job_index] = max(attempts[job_index], used)
                try:
                    findings = parse_findings(text, reviewer=job.spec.name)
                except ValueError as exc:
                    failures[job_index].append(f"unparseable findings: {_short_error(exc)}")
                    findings = []
                for finding in findings:
                    collected[job_index].append((finding, text))
            self._emit("reviewer.done", {"reviewer": job.spec.name, "chunk": chunk_index,
                                         "findings": len(findings)})

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="egm-swarm") as pool:
            list(pool.map(work, units))

        outcomes: list[ReviewerOutcome] = []
        for job_index, job in enumerate(jobs):
            findings = _merge_chunks([f for f, _ in collected[job_index]])
            status = STATUS_COMPLETED if not failures[job_index] else STATUS_NOT_RUN
            if failures[job_index] and findings:
                # Partial coverage is reported as completed only when every chunk
                # that ran produced a report and no chunk is missing.
                status = STATUS_COMPLETED if len(raws[job_index]) >= job.chunks else STATUS_NOT_RUN
            outcomes.append(ReviewerOutcome(
                spec=job.spec, status=status, findings=tuple(findings),
                raw="\n\n".join(raws[job_index]), error=" / ".join(failures[job_index]),
                attempts=attempts[job_index], chunks=job.chunks,
                elapsed=time.time() - started,
            ))
        self._emit("swarm.done", {"outcomes": [o.as_dict() for o in outcomes],
                                  "elapsed": round(time.time() - started, 2)})
        return outcomes

    # ── internals ─────────────────────────────────────────────────────────

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, payload)
            except Exception:  # noqa: BLE001 - a broken listener must not fail a review
                pass

    def _run_unit(self, job: ReviewJob, bundle: PromptBundle, chunk_index: int) -> tuple[str, int]:
        last_error: Exception | None = None
        attempts = self.config.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._launch_once(job, bundle, chunk_index, attempt), attempt
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if _no_parent(exc):
                    # The lifecycle facade cannot see a parent in this execution
                    # context. The delegate_task path resolves its parent from the
                    # CLI/gateway reference instead, so use it rather than failing.
                    self._emit("reviewer.fallback", {"reviewer": job.spec.name,
                                                     "reason": _short_error(exc)})
                    return self._run_via_delegate(bundle), attempt
                if _is_fatal(exc):
                    raise
                self._emit("reviewer.retry", {"reviewer": job.spec.name,
                                              "chunk": chunk_index, "attempt": attempt,
                                              "error": _short_error(exc)})
                time.sleep(min(2.0 * attempt, 5.0))
        raise SwarmError(_short_error(last_error) if last_error else "reviewer failed")

    def _run_via_delegate(self, bundle: PromptBundle) -> str:
        """Run one reviewer through ``delegate_task`` when no lifecycle parent exists."""
        shim = _DelegateShim(self._ctx, self.config)
        handle = shim.launch({"goal": bundle.goal, "context": bundle.context})
        shim.wait(handle, timeout_seconds=self.config.timeout)
        result = shim.result(handle)
        text = getattr(result, "summary", "") or ""
        if not str(text).strip():
            raise SwarmError("delegate_task returned no reviewer report")
        return str(text)

    def _launch_once(self, job: ReviewJob, bundle: PromptBundle, chunk_index: int, attempt: int) -> str:
        service = self._service()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        correlation = (f"egm-{self._run_token}-{sequence}-{job.spec.name}-"
                       f"{chunk_index}-{attempt}")
        request = self._request(job, bundle, correlation)
        try:
            handle = service.launch(request)
        except Exception as exc:  # noqa: BLE001
            # A host that refuses ("file") because the parent narrowed its own
            # toolsets still accepts an empty request: children inherit the parent.
            if "broaden" in _short_error(exc).lower() and isinstance(request, dict) and request.get("allowed_toolsets"):
                request = dict(request)
                request.pop("allowed_toolsets", None)
                handle = service.launch(request)
            else:
                raise
        terminal = service.wait(handle, timeout_seconds=self.config.timeout)
        if getattr(terminal, "timed_out", False):
            try:
                service.cancel(handle, reason="egm reviewer timeout")
            except Exception:  # noqa: BLE001
                pass
            raise TimeoutError(f"reviewer timed out after {int(self.config.timeout)}s")
        result = service.result(handle)
        state = str(getattr(getattr(result, "terminal_state", ""), "value",
                            getattr(result, "terminal_state", ""))).upper()
        if not getattr(result, "ready", False) or not state.endswith("SUCCEEDED"):
            detail = getattr(result, "error_message", "") or getattr(result, "error_classification", "") or state
            raise SwarmError(f"reviewer child ended {state or 'unknown'}: {detail}")
        summary = getattr(result, "summary", None) or ""
        if not str(summary).strip():
            raise SwarmError("reviewer child returned an empty report")
        return str(summary)

    def _request(self, job: ReviewJob, bundle: PromptBundle, correlation: str) -> Any:
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest
            request: Any = SubagentLaunchRequest
        except Exception:  # noqa: BLE001 - mapping form works on every version
            request = None
        values = {
            "goal": bundle.goal,
            "context": bundle.context,
            "role": "leaf",
            "correlation_id": correlation,
            "metadata": {"plugin": "egm", "reviewer": job.spec.name},
        }
        if self.config.model:
            values["model"] = self.config.model
        if self.config.provider:
            values["provider"] = self.config.provider
        toolsets = self._toolsets()
        if toolsets:
            values["allowed_toolsets"] = toolsets
        if request is None:
            return values
        try:
            return request(**values)
        except TypeError:
            return values

    def _toolsets(self) -> tuple[str, ...]:
        # Read access is all a reviewer needs. "file" is the narrowest built-in
        # toolset that carries read_file/search_files; write stays prompt-level
        # forbidden, exactly as upstream personas specify.
        return ("file",)

    def _service(self) -> Any:
        service = getattr(self._ctx, "subagent_lifecycle", None)
        if service is None:
            return _DelegateShim(self._ctx, self.config)
        return service


class _DelegateShim:
    """Fallback for a core without ``ctx.subagent_lifecycle``.

    Uses the ``delegate_task`` tool through the plugin context, which is the
    documented harness fallback in the upstream skills. It is deliberately
    narrow: one child per call, no sampling controls, no lifecycle handles.
    """

    def __init__(self, ctx: Any, config: SwarmConfig) -> None:
        self._ctx = ctx
        self._config = config

    def launch(self, request: dict[str, Any]) -> dict[str, Any]:
        raw = self._ctx.dispatch_tool("delegate_task", {"tasks": [{
            "goal": request["goal"], "context": request.get("context", ""),
        }]})
        return {"raw": raw}

    def wait(self, handle: dict[str, Any], *, timeout_seconds: float | None = None) -> Any:
        return _Terminal(False)

    def result(self, handle: dict[str, Any]) -> Any:
        return _Result(_extract_summary(handle.get("raw")))

    def cancel(self, handle: dict[str, Any], *, reason: str) -> None:
        return None


@dataclass(frozen=True)
class _Terminal:
    timed_out: bool


@dataclass(frozen=True)
class _Result:
    summary: str
    terminal_state: str = "SUCCEEDED"
    ready: bool = True
    error_classification: str = ""
    error_message: str = ""


def _extract_summary(raw: Any) -> str:
    """Pull child summaries out of a ``delegate_task`` JSON result."""
    if isinstance(raw, (dict, list)):
        data = raw
    else:
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            return str(raw or "")
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("summary", "result", "output", "text"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return "\n\n".join(dict.fromkeys(texts)) if texts else str(raw or "")


def _merge_chunks(findings: list[Finding]) -> list[Finding]:
    """Merge per-chunk findings without losing distinct issues."""
    seen: set[tuple[str, str, str]] = set()
    merged: list[Finding] = []
    for finding in findings:
        key = (finding.severity, finding.scope, finding.description.casefold()[:120])
        if key in seen:
            continue
        seen.add(key)
        merged.append(finding)
    return merged


def _no_parent(exc: Exception) -> bool:
    text = _short_error(exc).lower()
    return "no active hermes parent" in text or "no active parent session" in text


def _is_fatal(exc: Exception) -> bool:
    return "not available" in _short_error(exc).lower() and "lifecycle" in _short_error(exc).lower()


def _short_error(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = str(exc).strip().replace("\n", " ")
    return text[:400] if text else exc.__class__.__name__


def build_swarm(ctx: Any, settings: dict[str, Any] | None = None,
                on_event: EventHook | None = None) -> SwarmRunner:
    """Build a runner from plugin settings (tolerant of strings and missing keys)."""
    settings = settings or {}

    def _int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    config = SwarmConfig(
        max_reviewers=_int("max_reviewers", 6, 1, 8),
        retries=_int("retries", 1, 0, 3),
        timeout=float(_int("review_timeout_seconds", 600, 60, 1800)),
        dual_run=bool(settings.get("dual_run", False)),
        model=str(settings.get("reviewer_model") or ""),
        provider=str(settings.get("reviewer_provider") or ""),
    )
    return SwarmRunner(ctx, config, on_event=on_event)
