# Architecture

## The two contexts

EGM's only non-negotiable mechanism is context separation. The **Elephant** (the
chat you are in) holds the project history, the requirements, and the decisions.
A **Goldfish** is a child session that receives the persona, the rubric, the
document slice, and a reading list — and nothing else. It cannot see the
conversation, so it cannot inherit its assumptions.

In Hermes that separation is provided by the public subagent lifecycle API:
`ctx.subagent_lifecycle.launch()` builds a real child session with its own
context window, `wait()` and `result()` return its report, and `cancel()` stops a
runaway reviewer. The plugin never imports delegation internals.

```
chat (Elephant)
  │  egm_review / egm_review_loop / egm_brainstorm / egm_drift
  ▼
review.py  ── stage plan ──► prompts.py ── bundle ──► swarm.py
  │                            (persona + rubric +     │
  │                             numbered slice +      │ launch N children
  │                             reading list)          ▼
  │                                              subagent lifecycle
  │                                                    │
  │  ◄──────── findings (JSON) ────────────────────────┘
  ▼
synth.py  normalize → dedupe → severity → render
  │
  ▼
reports.py  stage report + consolidated report on disk
```

## Module responsibilities

| Module | Owns | Must not |
| --- | --- | --- |
| `types.py` | the vocabulary | know about files or models |
| `catalog.py` | which persona/rubric each stage uses | read documents |
| `documents.py` | markdown structure, semantic targets, link containment | call a model |
| `prompts.py` | what a child sees, and the context budget | launch children |
| `swarm.py` | launching, waiting, retrying, status truth | decide severity |
| `synth.py` | normalization, dedupe, report layout | run reviews |
| `state.py` | round memory and convergence verdicts | edit documents |
| `review.py` | stage orchestration and artifact placement | format findings |
| `lint.py` | mechanical checks with no model | duplicate reviewer judgment |
| `bootstrap.py` | tree discovery and partitioning | write files |
| `tools.py` / `slash.py` / `cli.py` | surfaces | contain business logic |

## Invariants

1. **A reviewer that did not run is `not run`, never "zero findings".** The report
   status table lists it, and a round containing one is not clean.
2. **Nothing from the parent conversation reaches a child.** The request carries
   `goal`, `context`, `role`, `correlation_id` and `metadata`; there is no field
   for history, and `metadata` is limited to the reviewer's name.
3. **The target document is not modified by the engine.** Reviewers are told they
   are read-only, but the shared `file` toolset still contains write tools, so the
   rule is enforced by the persona. The engine itself writes only report artifacts.
4. **Reports are files, not chat.** The tool result is a compact JSON summary with
   paths; the skill tells the user where to look.
5. **The five stages are data, not prose.** `catalog.STAGES` is the single source
   for reviewer lists, ranges and filenames.
6. **Findings are stable across rounds.** `state.fingerprint` ignores severity and
   line drift, so a settled issue cannot be re-opened by a fresh reviewer that
   grades it differently.

## Context budgeting

The host caps a child's `context` at 32,000 characters. Composition is measured,
not estimated:

1. Build the full bundle for the requested range.
2. If it fits the configured budget (default 26,000), launch one child.
3. If not, split the range at heading boundaries and build bundles per chunk.
4. If a single line is still too large, truncate it with an explicit marker.

A reviewer covering several chunks has its findings merged and deduplicated; the
report records how many chunks it took. Reading-list files get half the budget and
are truncated individually with a visible marker.

## Convergence

`state.py` implements the upstream loop rules literally:

| Rule | Implementation |
| --- | --- |
| Settled finding | `state.settle()` records a fingerprint; later rounds ignore it |
| Fixed finding re-opens | fixes are not settlements; a new report of the same issue is a new finding |
| Clean round | no non-settled Blocker/Major **and** every reviewer ran |
| Converged | two consecutive clean rounds |
| Cap | 5 rounds by default |
| Churn | a new Blocker/Major at a scope marked fixed in an earlier round |

`egm_review_loop` takes `settled` (finding ids from earlier rounds) and
`fixed_scopes`; the engine applies both before recording the round. That is what
makes the settled and churn rows above real: an accepted risk stops blocking, and
a regression on a fixed scope is flagged.

The verdict is advisory but explicit: `{clean, converged, cap_reached, churn,
reason}`. The driver stops on `converged`, and asks the user on `cap` or `churn`,
exactly as the upstream skill specifies.

## Extension points

- **New reviewer**: add a persona file, one `ReviewerSpec` in `catalog.py`, and a
  row in the docs. No engine change.
- **New rubric**: drop a file in `standards/`; pass it by shortname.
- **New stage**: add a `Stage` to `catalog.STAGES` and a preset tuple.
- **Different model for the reviewer swarm**: set `reviewer_model` /
  `reviewer_provider` in plugin settings, or route through a Hermes delegation
  provider.
- **Different artifact location**: `report_dir` setting.

## What the host does not allow

- Per-launch timeouts (`timeout_seconds` is rejected by the lifecycle API), so the
  plugin enforces its own wait budget and cancels the handle.
- Per-tool blocks, so read-only reviewers rely on the persona plus the narrowest
  built-in toolset (`file`), which still contains write tools. Upstream has the
  same posture.
- Launching outside an agent turn, so reviews run from a chat or a tool call, not
  from `hermes egm` on the command line. The CLI covers every deterministic
  operation instead.
