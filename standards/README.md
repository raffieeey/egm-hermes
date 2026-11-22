# Document standards

Five evaluation rubrics, copied verbatim from the upstream
`resources/document_standards/` directory of
[google/elephant-goldfish-model](https://github.com/google/elephant-goldfish-model)
(Apache-2.0).

| File | Shortname | Applied by default to |
| --- | --- | --- |
| `feature-spec-standard.md` | `feature-spec` | `design`, `structural` |
| `implementation-readiness-standard.md` | `implementation-readiness` | `implementation_readiness`, `codebase_details` |
| `zoom-out-standard.md` | `zoom-out` | `zoom_out` |
| `heilmeier-standard.md` | `heilmeier` | any reviewer, when a proposal needs the catechism |
| `re-implementation-standard.md` | `re-implementation` | any reviewer documenting existing code |

`comprehension` and `style` run without a standard by default, as upstream does.
Pass `standards={"style": "re-implementation-standard"}` to the review tool to
bind one.
