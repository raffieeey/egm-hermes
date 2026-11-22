# Reviewer personas

Ten stateless reviewer prompts, copied verbatim from the upstream
`agents/` directory of
[google/elephant-goldfish-model](https://github.com/google/elephant-goldfish-model)
(Apache-2.0). They are not edited here: the whole point of a Goldfish is that its
instructions are fixed and known.

| File | Reviewer shortname | Used by |
| --- | --- | --- |
| `zoom_out.md` | `zoom_out` | Stage 5, Stage 8, brainstorm lens |
| `comprehension.md` | `comprehension` | Stage 6, brainstorm lens |
| `design_critic.md` | `design` | Stage 7, brainstorm lens |
| `structural.md` | `structural` | Stage 7, brainstorm lens |
| `style.md` | `style` | Stage 7, Stage 9, brainstorm lens |
| `implementation_readiness.md` | `implementation_readiness` | Stage 9, brainstorm lens |
| `codebase_details.md` | `codebase_details` | Stage 9, drift, brainstorm lens |
| `inventive_catalyst.md` | `inventive_catalyst` | Stage 5, brainstorm lens |
| `synthesizer.md` | — | Aggregation contract the engine implements in code |
| `bootstrap_orchestrator.md` | — | Bootstrap worker persona |

The engine reads the file verbatim and injects it into the child's prompt. To
change reviewer behavior, edit the persona file or override the rubric with the
`standards` argument; there is no hidden second copy of the instructions.
