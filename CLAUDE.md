# Orchestration contract, RM6116 knowledge graph build

This file governs how the agent fleet builds this repo. What to build lives in `handover/SPEC.md`. Why it is shaped that way lives in `DESIGN.md` and `EVALUATION.md`. Read all three in full before delegating or writing code. Dan is the operator, he reviews increments as they land and his decisions override this file.

## Roles

- **Orchestrator, the main session.** Plans, decomposes, delegates, reviews, integrates. Writes no feature code except `config.py`, `pipeline/schemas.py` and `fixtures/`, which it owns and commits before any worker starts. Keeps `handover/REVIEW-NOTES.md` current, what came back from each worker, what was accepted, rejected or changed, and why.
- **Workers, Opus subagents** defined in `.claude/agents/`, `parser-builder`, `resolver-builder`, `enrichment-builder`, `eval-builder`, `ui-builder`, `researcher`, plus the `reviewer` and `tester` gate roles. `CLAUDE_CODE_SUBAGENT_MODEL` in `.claude/settings.json` pins workers to Opus.

## Coordination rules

1. **Contracts first.** `schemas.py`, `config.py` and `fixtures/` are committed before any worker spawns. Workers build against them and never silently change them. If a contract is wrong, stop and report.
2. **Decompose by file ownership.** The ownership map in `handover/SPEC.md` section 1 is exact. No two workers touch the same file, cross cutting needs go through the orchestrator.
3. **One worker, one worktree, one branch.** Builders run with worktree isolation. Nobody edits the main checkout except the orchestrator, and nobody merges their own branch.
4. **Verify before merge.** The tester runs the real gate, pytest plus the pipeline end to end on batch B1, and reports real output. The reviewer reads the diff adversarially, correctness, scope, contract drift, missing tests. Trust command output, never a worker's self report.
5. **Integrate through one gate.** The orchestrator merges branches sequentially, re running the gate after each.
6. **Maximise safe parallelism.** Parts fan out in stages 1 and 2. Stages 3 to 6 share inputs and are independent, so resolver-builder and enrichment-builder run concurrently once trees exist. The ui-builder starts immediately against `fixtures/` and never waits for the pipeline. The researcher runs from the start, its memo has no dependencies. Only delay work that genuinely depends on missing output.
7. **Show progress continuously.** Report to Dan as components complete, not at the end. Every finished component ships with something runnable or viewable, a CLI run on real pages, an eval report, a published design canvas URL. Testable increments beat silent completeness.
8. **Commit and push as you go.** Small commits on worker branches, orchestrator pushes `master` after each integration. The remote is the backup.

## Standards that are not negotiable

- Fidelity. Source text is never cleaned, corrected or paraphrased anywhere in the pipeline. Anomalies are recorded beside the raw text, interpretations are flagged as interpretations.
- Provenance. Every node keeps its page, boxes and batch. Anything that cannot cite its page does not go in the graph.
- Determinism where the spec says deterministic, byte for byte. LLM calls only through `pipeline/llm.py`, logged and replayable.
- Honesty. Never claim a test passed without showing its output. Never fabricate counts, metrics or examples. If something is unverified, say so in the report.
- Secrets. `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from the environment or the gitignored `.env`, never printed, never committed.
- The spec wins over instinct. Changes to the spec are made by the orchestrator, in `handover/SPEC.md` first, then `schemas.py`, then code, recorded in `handover/REVIEW-NOTES.md`.

## UI design process

Both UIs are designed before they are coded, with Claude Design (the `/design` skill), artboards under `design/`, two or three directions per key screen on one canvas, one chosen with a recorded sentence of reasoning, the approved artboard the source of truth thereafter. Publish each canvas as an Artifact and put the URL in progress notes.

## Definition of done, per worker

The worker's stage CLIs run clean on their inputs, outputs validate against `schemas.py`, unit tests exist and pass, the tester gate is green, the reviewer found nothing unaddressed, and the worker's report names the exact files touched, the real command output, and anything the spec did not cover.
