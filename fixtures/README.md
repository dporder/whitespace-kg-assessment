# Fixtures

Hand-made stage outputs, orchestrator owned, committed before any worker
started. Downstream builders (eval, UIs, resolver, enrichment) develop against
these until real pipeline output lands, then swap to `output/` by one config
value. `make_fixtures.py` is the curated source of truth, regenerate with:

```
.venv/bin/python fixtures/make_fixtures.py
```

Two decisions worth knowing:

- **The text is synthetic mimicry, not copied from the PDF.** The SPEC ground
  rule forbids copying document content into the repo outside `output/`, so the
  fixtures reproduce the structures, not the words: the bare grouping
  sub-heading (numbering says clause level, function says heading), the
  intro-plus-items sandwich with a ref-bearing intro, a list phrase split into
  grouped refs, an unresolved ref whose target part has not arrived, an
  ambiguous bare `Schedule 2`, an external legislation ref, the two-column
  definitions table with a delegating definition and a parenthetical alias,
  form rows with `[Insert ...]` placeholders and the stray-character label
  typo, and a sentence-initial ambiguous term use.
- **Geometry is fabricated but invariant-clean** (children indent right of
  parents, own box above first child, siblings ascend, extents nest). Page
  numbers are fixture-local, so a UI crop rendered against the real PDF shows
  real ink from those coordinates, not this text. That is fine for layout
  development and the crop path becomes truthful the moment real `output/`
  trees land.

Files:

- `tree/<part>.json` — stage 2 shape: one part `Node` per file, **no ref
  children** (refs attach at stage 7).
- `refs/<part>.json` — stage 3 shape: `RefsFile`, flat list of ref nodes whose
  paths are `parent-path/ref@start-end`.
- `vocab/definition_sites.json`, `vocab/term_uses.json` — stage 4 shapes.
- `concepts.json` — stage 5 shape.
- Embeddings have no fixture: vector files are heavy and nothing downstream
  needs one to start. The `EmbeddingRecord` schema plus the cache layout in
  SPEC 2.4 is the contract.

`tests/fixtures/test_fixtures_validate.py` re-validates every file against
`pipeline/schemas.py` and checks span integrity (every ref and term-use span
reproduces its surface text from its node's text).
