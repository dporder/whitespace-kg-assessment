# RM6116 into a knowledge graph, a Scalable Approach

## Where to look

| | |
|---|---|
| `diagram/final-diagram-export/rm6116-diagram.pdf` | The diagram, two pages. The system, then one question walked through the graph. The two `.dc.html` files beside it are its source. |
| `DESIGN.md` | The reasoning behind the diagram. Data model, pipeline, scale, the calls I made and the ones I deferred. |
| `EVALUATION.md` | How I would know the graph is right. Layers, gates, ground truth, error costs. |
| `handover/` | What the agent fleet was given and how it was run. `SPEC.md` is the contract they built against, `KICKOFF.md` the prompt that started the build session, `REVIEW-NOTES.md` my running record of what I accepted, rejected and corrected. |
| `CLAUDE.md` | The operating contract for the orchestrator and workers. |
| `.claude/agents/` | The worker role definitions. |
| `pipeline/` | The build. Stages land here as their branches pass the reviewer and tester gates. |
| `docs/research/` | Research memos produced by agents and verified before acceptance. |

## Running it

Python 3.12, dependencies in the venv per `handover/SPEC.md` section 0. Each stage is a CLI,
`python -m pipeline.<stage>`, contracts and batch definitions in `config.py`. Keys for the
model stages come from a gitignored `.env` and are never committed.
