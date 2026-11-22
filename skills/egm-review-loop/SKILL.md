---
name: egm-review-loop
description: "Iterate Goldfish review rounds until findings converge."
version: 1.0.0
license: Apache-2.0
metadata:
  hermes:
    tags: [egm, review, loop]
---

# Goldfish Review: Loop (Orchestration Driver)

## Overview

This skill acts as the central orchestration engine driving the multi-agent
review loop. It coordinates two distinct agent memory models:

*   **The Goldfish (Adversarial Auditors)**: Subagents spawned in isolated,
    fresh context windows to catch unstated assumptions without historical bias.
*   **The Elephant (Persistent Resolver)**: The primary agent context holding
    institutional knowledge to evaluate findings and apply approved repairs.

The driver manages argument intake, resolves paths for reviewers and standards,
unconditionally re-parses target line ranges before every iteration, spawns
concurrent specialized Goldfish auditors based on configuration, and coordinates
with the Elephant context to resolve findings.

## Delegation

A Goldfish runs in a fresh context with no conversation history. In Hermes, call the `egm_review` tool (single stage or preset) or `egm_review_loop` tool (one iterative round). Both launch isolated children through the public subagent lifecycle; each child receives only its persona, its rubric, the target slice, and the reading list. Only if those tools are unavailable, fall back to `delegate_task` and tell each child to read its persona from the plugin's `personas/` directory first. A review in the same context is not a Goldfish review: stop and say so.

## Inputs

The caller or user provides exactly these parameters:

*   **`TargetFile`**: Absolute or relative path to target document.
*   **`SemanticTarget`**: Descriptive targeting string (e.g., *"Target the H4
    block with SS-ID 'S1.12'"*), or `full file`.
*   **`Reviewers`**: List of reviewer names (shortnames or paths).
*   **`Standards`**: Mapping of reviewer names to standard names (shortnames or
    paths).
*   **`GoldfishContext`**: A reading list (source files, directories, or
    documentation URLs) fed to reviewers to establish their baseline. An empty
    list from the caller counts as provided; do not ask for it again. Some
    reviewers cannot open URLs, so before the first round save the raw page of
    each URL as a new file in the target's directory (or the configured
    `report_dir`) and pass its absolute path instead. If a page cannot be
    saved, ask the user for a local copy and end your turn; if no one can
    answer, as in a headless run, leave it out, tell the user, and keep going.

Optional parameters, used from the second round onward:

*   **`SettledFindings`**: Finding ids the user accepted or contested in earlier
    rounds, passed to the tool as `settled`. Settled findings stop counting
    against convergence.
*   **`FixedScopes`**: Scopes whose fixes were applied since the last round,
    passed to the tool as `fixed_scopes`. A new Blocker or Major there is churn.
*   **`ReportName`**: Filename for this round's artifact, default
    `goldfish_review_report.md`. The caller may name it `critic_report.md` or
    `implementation_readiness_report.md` to match the stage it drives.

## Path & Standard Resolution Logic

When resolving names in `Reviewers` and `Standards`:

*   **Reviewer Shortnames**: Map reviewer shortname (e.g., `design_critic`,
    `structural`, `style`, `implementation_readiness`,
    `codebase_details`, `comprehension`, `zoom_out`) to `personas/{name}.md`.
    `design` and `design_critic` both resolve to `personas/design_critic.md`.
    The persona file defines the reviewer's core mandates, audit vectors, and
    output format.
*   **Standard Shortnames**: Map standard shortname (e.g., `feature-spec-standard`,
    `implementation-readiness-standard`, `zoom-out-standard`, `heilmeier-standard`,
    `re-implementation-standard`) to `standards/{name}.md`.
*   **Custom Paths**: If the name contains a slash (`/`), treat it as an
    absolute or workspace-relative path and read it directly.

## Execution Workflow

### Step 1: Intake & Configuration UX

Evaluate the provided inputs (`TargetFile`, `SemanticTarget`, `Reviewers`,
`Standards`, `GoldfishContext`).

1.  **Identify Missing Inputs**: Determine which of the required parameters are
    missing.
2.  **Interactive Onboarding**:
    *   If **all** inputs are present, proceed to Step 2.
    *   If **some or all** inputs are missing, interactively prompt the user
        **only for the missing inputs**, one at a time via chat messages.
3.  **Reviewer Recommendations**:
    *   *Feature Specs*: Recommend `design_critic` (mapped to
        `standards/feature-spec-standard.md`) and
        `structural` (mapped to `standards/feature-spec-standard.md`).
    *   *Implementation Plans*: Recommend `implementation_readiness`
        (mapped to `standards/implementation-readiness-standard.md`)
        and `codebase_details`.
4.  **Confirm Configuration**: Display the consolidated plan (Target, Reviewers,
    and mapped Standards) and ask the user to confirm before proceeding.

### Step 2: The Sharpening Loop (Iterative Cycle)

One invocation of `egm_review_loop` runs exactly one round and persists round
state: the round number, the settled findings, the applied fixes, and the
clean-round count. The driver calls it again only when the user confirms. The
sequence below describes one round.

#### 2.1 Unconditional Target Resolution

Before every iteration, unconditionally re-scan `TargetFile` by reading it
and using the stable `SemanticTarget` string (e.g., locating the target header) to
dynamically determine the updated absolute start and end line numbers. This
ensures absolute accuracy even if the file was edited in prior rounds. (If `full
file` mode is specified, confirm full-file line bounds.) The tool performs this
at the start of every round; when you dispatch by hand, do it yourself.

#### 2.2 Review Swarm Execution

1.  **Launch Subagent Swarm**:
    Call
    `egm_review_loop(target=TargetFile, reviewers=Reviewers,
    standards=Standards, semantic_target=SemanticTarget,
    context=GoldfishContext, max_rounds=5, settled=SettledFindings,
    fixed_scopes=FixedScopes, report_name=ReportName)`. It runs this round's
    swarm and returns `{ok, round, counts, verdict, findings, settled_applied,
    fixed_scopes, report_path}`.
    *   **Hierarchical Dispatch (Synthesizer)**: With synthesizer dispatch, the
        intermediate `synthesizer` persona spawns the Goldfish review swarm in
        parallel, deduplicates findings, writes the report artifact, and returns
        a single summary message to avoid context pollution in the root
        conversation. Pass it the Failed Reviewers rule below and
        `GoldfishContext`, and tell it to give those files to each reviewer. Its
        summary must give a status, `completed` or `not run`, for each requested
        reviewer.
    *   **Direct Dispatch (Flat Swarm)**: Alternatively, spawn each requested
        reviewer subagent in `Reviewers` directly, or fall back to
        `delegate_task` when the tools are unavailable.
        Instruct each subagent to first read its persona definition in
        `personas/{name}.md`, then audit `TargetFile` at the resolved line range
        against its designated standard in `standards/`, supplying
        `GoldfishContext`.
    *   Each reviewer produces findings with severity (🔴 Blocker, 🟡 Major, 🟢
        Nit), line numbers, discrepancy descriptions, and rationales.
2.  **Consolidate Findings**: Collect all findings across reviewers into a single
    flat `FindingsList`. Give each finding a stable id
    (`<reviewer>-<round>-<n>`) and record its reviewer, severity, and affected
    scope. The tool returns this list in `findings`; when you dispatch directly,
    build it yourself.
3.  **Failed Reviewers**: A reviewer that times out, fails, or returns no report
    did not review. Retry it once. If it fails again, record it as **not run**
    in the report. Never count it as a reviewer with zero findings. If the
    synthesizer summary has no status for a requested reviewer, record that
    reviewer as not run.

#### 2.3 Aggregated Resolution Hand-off

1.  Pass `TargetFile`, the active line range, and the aggregated `FindingsList`
    to the **`egm-resolve`** workflow (a skill, not a tool).
2.  Execute the resolution workflow in the primary Elephant context:
    *   Categorize findings into `CONTEST` vs. `PROPOSE FIX`.
    *   Save `goldfish_review_report.md` with `write_file`.
    *   Present the interactive decision gate (Auto accept, Continue, Interactive
        breakdown).
    *   Apply approved text repairs to `TargetFile` using `patch` and
        `write_file`.
    *   Collect the ids of contested and accepted-risk findings into
        `SettledFindings`, and the scopes of applied fixes into `FixedScopes`,
        for the next round.

#### 2.4 Iteration Check & Turn Boundary

Fresh reviewers sample differently each round, so a round with zero findings
is rare even when the document improves. Use these rules instead:

*   **Settled findings**: A finding is settled when it is contested or
    recorded without a fix (accepted risk). Keep the settled list across
    rounds. If a new reviewer raises a settled finding again (same affected
    scope, same issue), do not re-open it.
*   **Fixed findings**: An applied fix does not settle a finding. If a later
    reviewer raises the same issue at the same scope, the fix failed or a
    later edit reintroduced the issue. Re-open the finding.
*   **Clean round**: A round is clean when no reviewer raises a Blocker or
    Major finding that is not settled. Nits do not block. A reviewer recorded
    as not run makes the round not clean. Decide this from the review results,
    before you apply the fixes of that round.
*   **Converged**: Two consecutive clean rounds. One clean round is not enough,
    because the next fresh reviewer can find a different gap in the same text.
*   **Cap**: 5 rounds.
*   **Churn**: A new finding is at the scope of an applied fix, and reverting
    that fix would resolve it.

The tool's `verdict` reports `clean`, `converged`, `cap_reached`, and `churn`
for the round just completed; `round` is its number and `counts` gives the
severity totals. Apply the rules above to decide whether another round is
allowed.

On converged, go to Step 3. On cap or churn, write a short list of the open
findings by severity, then ask the user one question: accept and proceed,
continue past the cap, or return the document to drafting.

**Crucial Turn-Taking Requirement**: After the resolution phase finishes for a
given round, the loop driver **MUST respect the turn-taking checkpoint in
`egm-resolve`**. The driver must remain idle across human turns,
only looping back to Step 2.1 when the user explicitly confirms they wish to run
another round of review. In Hermes terms, one `egm_review_loop` call is one
round: never call it twice in the same turn, and never chain rounds without that
confirmation.

### Step 3: Output Boundary & Final Checkpoint

Upon completion (converged, cap or churn accepted by the user, or manual
halt):

*   **Programmatic Callers**: If running as a subflow of `egm-review`, exit
    cleanly to hand execution back to the parent workflow.
*   **Manual Invocations**: Output a summary of completion in chat: *"The review
    loop has completed after [N] rounds ([converged | accepted at cap | halted]).
Open findings are in the report. Would you like to proceed to
    the next stage, or do you have any final questions?"*
