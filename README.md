# Elephant-Goldfish Model for Hermes

**Design with an elephant. Review with a goldfish.**

A Hermes Agent plugin that ports [google/elephant-goldfish-model](https://github.com/google/elephant-goldfish-model):
the same eight workflow skills, ten reviewer personas and five document
standards, with the review pipeline implemented as a plugin engine.

The point of the method is context separation. Drafting happens in a session that
holds the whole project. Review happens in sessions that hold only the document,
its rubric and a reading list. A reviewer that never saw the discussion cannot
inherit its assumptions.

| Role | Context | Responsibility |
| --- | --- | --- |
| **Elephant** | Project, requirements, earlier decisions | Explore the problem and draft the specification |
| **Goldfish** | Only the document, rubric and reading list | Find missing assumptions, unclear requirements, implementation risks |
| **You** | Goals and constraints | Resolve trade-offs, approve the design, decide what to build |

## Install

```sh
hermes plugins install raffieeey/egm-hermes
```

Start a new session so the skills and tools load, then check the install:

```sh
hermes plugins list | grep egm
hermes egm selftest
```

## Quick start

Review an existing design document, README or PR plan against the code:

```
/egm:egm-review docs/designs/webhooks.md
```

Or the bare alias:

```
/egm-review docs/designs/webhooks.md
```

The agent runs the five stages and writes each report beside the document:

```
zoom_out_report.md
comprehension_report.md
critic_report.md
intermediate_zoom_out_report.md
implementation_readiness_report.md
consolidated_review_report.md
```

You get the paths and severity counts in chat. Findings stay in the files.

To design something new:

```
/egm-brainstorm Add webhook delivery to this project.
/egm-draft
/egm-review design_draft.md
```

## The five stages

| Stage | Name | Reviewers | Range | Report |
| --- | --- | --- | --- | --- |
| 5 | Initial Zoom Out | `zoom_out`, `inventive_catalyst` | full document | `zoom_out_report.md` |
| 6 | Comprehension Test | `comprehension` | full document, no reading list | `comprehension_report.md` |
| 7 | Critic Review | `design`, `structural`, `style` | feature specification | `critic_report.md` |
| 8 | Intermediate Zoom Out | `zoom_out` | full document | `intermediate_zoom_out_report.md` |
| 9 | Implementation Readiness | `implementation_readiness`, `codebase_details`, `style` | implementation plan | `implementation_readiness_report.md` |

The split between specification and implementation plan is found from
`## Detailed Implementation Plan`, or the usual synonyms. Ambiguous splits are
reported with the candidate headings rather than guessed.

## Skills

| Skill | Use it to |
| --- | --- |
| `egm:egm-brainstorm` | Test an idea across seven lenses and write a concept brief |
| `egm:egm-draft` | Turn a brief into a design document, section by section |
| `egm:egm-review` | Run the five-stage review |
| `egm:egm-review-loop` | Iterate rounds until findings converge |
| `egm:egm-resolve` | Triage findings and apply approved fixes |
| `egm:egm-bootstrap` | Generate README contracts across a codebase tree |
| `egm:egm-lite` | Fast loop for small changes: critique, checklist, review |
| `egm:egm-drift-report` | Find where checked-in documents diverge from the code |

Hermes exposes each skill as a slash command, so `/egm:<skill>` works on the CLI,
TUI, desktop app and messaging surfaces.

## Tools

| Tool | What it does |
| --- | --- |
| `egm_review` | Run a preset (`full`, `feature_spec`, `implementation`) or one stage (`5`–`9`) |
| `egm_review_loop` | Run one iterative round and return a convergence verdict |
| `egm_brainstorm` | Run the seven-lens concept swarm and write the brief |
| `egm_drift` | Verify every path, symbol, endpoint and table a document mentions |
| `egm_lint` | Mechanical checks: placeholders, fences, heading jumps, links, required sections |
| `egm_bootstrap_plan` | Discovery, pruning and the adaptive 3–5 way partition plan |
| `egm_status` | Recorded rounds, settled findings and the current verdict |

## CLI

```sh
hermes egm path                    # plugin root
hermes egm personas                # reviewer inventory
hermes egm standards               # rubrics
hermes egm stages                  # the five-stage pipeline
hermes egm plan docs/design.md     # semantic split and stage plan, no model calls
hermes egm lint docs/design.md     # deterministic checks
hermes egm drift docs/designs/     # verify mentions against the tree
hermes egm bootstrap .             # partition plan for README work
hermes egm status docs/design.md   # loop state
hermes egm selftest                # asset integrity
```

Add `--json` for machine-readable output. Reviews themselves run in a session,
because launching reviewer children requires an active turn.

## Configuration

Settings live under `plugins.entries.egm.settings`; edit them with
`hermes plugins config egm`.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_reviewers` | `6` | Reviewer children launched at once per stage |
| `reviewer_model` | `""` | Model override for reviewers; empty inherits the delegation config |
| `reviewer_provider` | `""` | Provider override for reviewers |
| `retries` | `1` | Retries for a reviewer that fails or returns no report |
| `max_context_chars` | `26000` | Prompt context budget |
| `review_timeout_seconds` | `600` | Wait per reviewer before it is recorded as not run |
| `dual_run` | `false` | Two independent instances per reviewer, for recall |
| `report_dir` | `""` | Fixed artifact directory; empty writes beside the target |

## How it works

1. The skill decides when to run a stage. The engine decides what a stage is:
   reviewers, range, rubric and artifact name.
2. `egm/documents.py` parses the document, resolves semantic targets such as
   `S1.12` or `## Technical Plan`, and finds the feature/implementation split.
3. `egm/prompts.py` builds one self-contained brief per reviewer. It measures the
   result and splits at heading boundaries when the host context cap would be
   exceeded, so a long document is chunked rather than truncated.
4. `egm/swarm.py` launches reviewers concurrently, waits with a budget, retries a
   failed reviewer once, and records `completed` or `not run`. A reviewer that
   did not run is never counted as a reviewer with zero findings.
5. `egm/synth.py` normalizes severities, assigns stable ids, deduplicates
   overlapping findings and renders the reports.
6. `egm/state.py` carries the iterative loop: settled findings, fixed scopes,
   clean rounds, convergence after two consecutive clean rounds, a five-round
   cap, and churn detection.

Design notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Parity with upstream

| Upstream | Here |
| --- | --- |
| `skills/egm-*/SKILL.md` | Same eight skills, harness mechanics adapted |
| `agents/*.md` personas | Copied verbatim (`personas/`) |
| `resources/document_standards/*.md` | Copied verbatim (`standards/`) |
| `resources/execution_instructions.md` | Kept inline in the draft and review constraints |
| `invoke_subagent` / `Agent` / `spawn_agent` | `ctx.subagent_lifecycle` through `egm_review` / `egm_review_loop` |
| `ask_question` gates | Hermes `clarify`, same one-question rule |
| Synthesizer subagent | `egm/synth.py`, deterministic and tested |
| Side-panel artifacts | Files on disk, paths in chat |
| Harness-specific plugin manifests | One native Hermes plugin |

## Limits

- Reviews need a live session. `hermes egm review` prints the command to run in
  chat rather than pretending to work.
- Read-only reviewers are a prompt rule plus the narrowest file toolset that
  still reads code. Hermes rejects per-tool blocks for child sessions; upstream
  has the same posture.
- Reviewer timeouts are client-side: the lifecycle API rejects per-launch
  timeouts, so the plugin waits with its own budget and cancels.
- `disable-model-invocation` has no Hermes equivalent, so skills are visible to
  the agent.
- `egm_brainstorm` fills the brief's Overview and Analysis from the reviewer
  results. Goals, Alternatives and Key Decisions are recorded with you during
  the session, as upstream requires.

## Development

```sh
python -m pytest -q
```

The suite covers document parsing and semantic targets, split discovery, link
containment, prompt budgeting and chunking, JSON recovery, deduplication,
severity normalization, report layout, the convergence state machine, drift
extraction, bootstrap partitioning, lint rules, the CLI, and a full five-stage
review against a fake host. No model calls required. CI runs it on Python 3.10
through 3.13.

## License

Apache-2.0. Personas, standards and templates are Google's, copied verbatim. The
skills are adapted for Hermes. The engine is new. See [NOTICE](NOTICE).

This is not an officially supported Google product, and this port is not
affiliated with or endorsed by Google.
