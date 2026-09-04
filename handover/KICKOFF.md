# Kickoff prompt, build session

You are the orchestrator for this repo. Operate under `CLAUDE.md` at the repo root, it is the contract for how this fleet works. Do this, in this order.

## 1. Load context, fully

Read, in full, in this order: `CLAUDE.md`, `handover/SPEC.md`, `DESIGN.md`, `EVALUATION.md`, `config.py`, `pipeline/schemas.py`, and skim `docs/research/legal-document-hierarchy.md`. Then spend ten minutes with the actual PDF, sample pages 3, 8, 24, 115 and 341 with PyMuPDF before writing any plan. Do not start delegating until you can state, in one paragraph each, the branch or leaf rule, the ref lifecycle, the batch and sweep semantics, and the quarantine conditions.

## 2. Verify the environment

The venv is `.venv/`. Check Neo4j is reachable on bolt://localhost:7687 and start it if not (`/opt/homebrew/bin/neo4j start`, set the password on first run and put it in `.env` as `NEO4J_PASSWORD`). Confirm `.env` at the repo root carries `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`, asking Dan for them if absent. That file is gitignored, never committed, never printed.

## 3. Commit contracts, then fan out

Reconcile `pipeline/schemas.py` and `config.py` against `handover/SPEC.md` sections 2 and 3 exactly, they must implement the unified Node schema with the ref kind, the discriminated union by kind, and the batch definitions. Build `fixtures/`, a small hand made tree for a few Core Terms clauses with refs, term uses and one form row, valid against the schemas. Commit all three. This unblocks every worker.

Then spawn in parallel, per the ownership map in SPEC section 1:

- `parser-builder`, stages 0 to 2, definition of done includes the full structural run over all 475 pages.
- `eval-builder`, stage 8 harness against `fixtures/` first, so the invariants and report exist before the pipeline does.
- `ui-builder`, both UIs against `fixtures/`, Claude Design artboards first per `CLAUDE.md`.
- `researcher`, the NER model selection memo for `docs/research/`, comparing zero shot span extractors against fine tuned token classifiers for the term matching scale path, primary sources, no code.

When trees exist for batch B1, spawn `resolver-builder` (stages 3 and 7) and `enrichment-builder` (stages 4, 5, 6) concurrently. Reviewer and tester gate every branch per `CLAUDE.md` rule 4.

## 4. Run the batches as the demonstration

In order: B1 Core Terms, B2 Joint Schedule 1, B3 Award Form, B4 Call-Off Schedule 9. B1 and B3 are the must haves if time compresses. After each batch, re run reference resolution over unresolved refs, run stage 8, and record the unresolved to resolved transitions, that sequence is the live demonstration of the second document story and it matters more than breadth. Also run `--full-structural` across all 475 pages and keep its report, the derived page map, the outline triage and the part count check are submission evidence.

## 5. Report cadence

After every integration, post to Dan in chat: what landed, the real command output that proves it, what is running now, any design canvas URLs, and anything needing his eyes, review queue samples especially. Keep `handover/REVIEW-NOTES.md` current as you accept, reject or fix worker output, it is a submission artifact. Small commits, push `master` after each integration.

## 6. Guard rails

Everything outside this repo is out of bounds except the assignment's `document/` directory, read only. Do not import the embedded outline or the provided page map anywhere except stage 8. Do not let any worker clean, correct or paraphrase source text. Do not report done without the eval report to show for it.
