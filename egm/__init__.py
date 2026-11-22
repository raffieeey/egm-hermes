"""Engine for the Elephant-Goldfish Model Hermes plugin.

Modules:
    types      shared vocabulary (Document, Finding, ReviewerOutcome, StageReport)
    catalog    reviewer, standard and stage registry
    documents  markdown parsing, semantic targets, linked-file containment
    prompts    reviewer prompt composition and chunking
    swarm      concurrent fan-out over Hermes's subagent lifecycle API
    synth      finding normalization and report rendering
    state      iterative-loop memory and convergence verdicts
    reports    artifact placement and atomic writes
    review     stage orchestration, brainstorm, loop, drift
    lint       deterministic document checks
    bootstrap  discovery, pruning and adaptive partitioning
    tools      tool schemas and handlers
    cli        ``hermes egm``
    slash      ``/egm`` and the bare workflow aliases
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
