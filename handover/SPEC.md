# SPEC. Build contract for the RM6116 knowledge graph pipeline

This is the frozen contract the agent fleet builds against. `DESIGN.md` at the repo root holds the reasoning. If this spec and a worker's instinct disagree, the spec wins. If the spec is wrong, stop and report, do not silently diverge. Schema changes happen here first, by the orchestrator, then in code.

## 0. Ground rules

- Python 3.12, the venv at `.venv/`. Dependencies already installed: pymupdf, neo4j, networkx, anthropic, fastapi, uvicorn, pydantic, rapidfuzz, pytest, python-dotenv, sentence-transformers.
- The PDF is at `../technical-assignment-provided-by-whitespace/document/RM6116 - Network Services 3 - Framework Agreement.pdf`. Treat it as read only. Copy nothing out of it into the repo except derived data under `output/`.
- `ANTHROPIC_API_KEY` is loaded from `../my-work/.env` via python-dotenv. Never print it, never commit it, never copy the file.
- Every stage is a CLI, `python -m pipeline.<stage>`, reading and writing JSON under `output/`. Exit code 0 on success, 2 on invariant violations (still writes output plus a violations file), 1 on failure.
- Determinism. Stages 0 to 2 and 4 must be pure functions of the PDF bytes and config. Same input, same output, byte for byte. No timestamps inside content, no dict ordering leaks. LLM stages (3 residue, 5, 6) record model, prompt version and raw responses under `output/<run>/llm_log/`.
- All LLM calls go through `pipeline/llm.py` (resolver-builder owns it, everyone imports it). It reads the key, sets the model per stage from `config.py`, retries with backoff, and logs every call.

## 1. Repository layout and ownership

```
solution/
  CLAUDE.md                orchestration contract (orchestrator owns)
  DESIGN.md  EVALUATION.md diagram/          (Dan and orchestrator own, workers read only)
  handover/                this spec, task briefs, kickoff prompt, logs, review notes
  config.py                slice definitions, model choices, paths      (orchestrator)
  pipeline/
    profile.py             Stage 0   parser-builder
    parse/                 Stage 1   parser-builder
    assemble/              Stage 2   parser-builder
    references/            Stage 3   resolver-builder
    vocabulary/            Stage 4   resolver-builder
    concepts/              Stage 5   resolver-builder (stretch, after 3 and 4 green)
    summaries/             Stage 6   resolver-builder (stretch)
    load/                  Stage 7   resolver-builder
    eval/                  Stage 8   eval-builder
    llm.py                 shared    resolver-builder
    schemas.py             pydantic models for every contract below (orchestrator, frozen)
  golden/                  hand labels, Dan writes, eval-builder consumes
  output/                  generated, gitignored
  tests/                   eval-builder
  review-ui/               ui-builder (after stages 1 to 4 merge)
  chat/                    ui-builder (after stage 7 merges)
  docs/research/           researcher output
```

One worker never edits another worker's files. Shared needs go through the orchestrator.

## 2. Data contracts

`pipeline/schemas.py` is the single source of truth and is committed before any worker starts. Prose summary of the shapes:

### 2.1 DocNode (stages 1 to 2 output, one tree file per part)

```json
{
  "id": "sha1(document + version + path), this version's instance",
  "lineage_key": "sha1(document + path), stable across versions",
  "content_hash": "sha1 of normalised own text, for version diffing",
  "path": "core-terms/3/3.1/3.1.2/a",
  "kind": "part | heading | clause | subclause | item | intro | form_row | table | cell | preamble",
  "unit_label": "Clause in Core Terms, Paragraph inside a Schedule, from the interpretation clause",
  "citable": true,
  "label": "3.1.2 or (a) or null",
  "title": "What needs to be delivered, headings only",
  "text": "own text only, never children's",
  "page_start": 3, "page_end": 3,
  "printed_page": "3, from the part's own footer",
  "bboxes_own": [{"page": 3, "bbox": [72.0, 401.2, 523.4, 445.0]}],
  "bboxes_extent": [{"page": 3, "bbox": [72.0, 401.2, 523.4, 620.5]}],
  "order": 17,
  "children": ["nested DocNodes"],
  "anomalies": ["strings, e.g. numbering_gap_after_3.2.9"]
}
```

Rules. A node has children or it has `text`, never both. Where the source gives a clause a lead in
sentence followed by sub paragraphs, emit the lead in as a first child of kind `intro` with
`citable: false`, and leave the parent's `text` null. Every other node is `citable: true`. The full
text of a node plus its descendants is a derived view, produced by walking the subtree in `order`,
and is never stored. Storing it would put the same sentence in the graph at three levels, would
make a change to one lettered paragraph dirty the content hash of every ancestor and so wreck the
version diff, and would leave the character offsets on references and term uses ambiguous about
which node owns them. Retrieval indexes may hold the denormalised text at whatever granularity
they want, since the search index is not the graph.

The leaf level is the deepest unit the document numbers, which is the lettered sub paragraph. Do
not split leaves into sentences. Sentence precision, where a UI wants it, comes from character
offsets into a leaf's text.

Boxes are the opposite. `bboxes_own` covers the node's own text, `bboxes_extent` covers the node
and everything under it, both as one entry per page touched, and overlap between a parent's extent
and a child's is expected rather than a problem. Storing both costs eight floats and saves a
subtree walk every time a viewer highlights a whole clause.

Geometric invariants, checked in stage 2 and reported in stage 8. These cross-check a tree that
was built from numbering against the geometry that built it, so they catch a mis-parented node
that the numbering alone would accept. A child's left edge is at or right of its parent's. A
node's own box sits at or above its first child's. Siblings do not overlap vertically on a page
and ascend in reading order. A node's extent stays inside its parent's extent. Violations are
recorded in `anomalies`, never repaired silently.

Headers, footers and printed page numbers are stripped from `text` but the printed page is kept as
a field. Tables keep cells as children with their own boxes. Form parts (Award Form) produce
`form_row` nodes with label and value cells. Placeholder text like `[Insert name]` is preserved
verbatim.

### 2.2 Reference (stage 3 output, one file per part)

```json
{
  "id": "sha1", "source_node_id": "...",
  "raw": "a Default of Clauses 2.10, 9, 14, 15, 27",
  "char_span": [120, 158],
  "kind": "clause | schedule | paragraph | annex | part | definition | legislation | unknown",
  "scope_rule": "js1_1.3.8 | js1_1.3.9 | title_paren | same_part | none",
  "status": "resolved | ambiguous | unresolved | external",
  "target_path": "core-terms/26 or null",
  "candidates": [{"path": "...", "score": 0.82}],
  "confidence": 0.97,
  "resolver": "regex | scope | llm | human",
  "expansion": ["2.10", "9", "14", "15", "27"]
}
```

A list like "Clauses 2.10, 9, 14, 15, 27" becomes one Reference per target via `expansion`. Ranges expand inclusively (JS1 1.3.10 says series are inclusive). Legislation references normalise to `{"title", "year", "kind", "provision"}` with `target_path` of
the form `legislation/bribery-act-2010` or `legislation/patents-act-1977/section/55`. Three shapes
occur and all three must parse, verified by counting them across the full document.

- Title plus year, 70 Act mentions and 18 Regulations mentions. Titles frequently contain
  parenthesised qualifiers that belong to the title and must survive normalisation, for example
  `European Union (Withdrawal) Act 2018` and
  `Transfer of Undertakings (Protection of Employment) Regulations 2006`. A greedy regex that stops
  at the first bracket will truncate these.
- A provision pointer into a statute, 22 occurrences, for example `Sections 55 and 56 of the Patents
  Act 1977` and `section 1162 of the Companies Act 2006`. These expand to one Reference per section
  the same way clause lists do, and the section goes in `provision`.
- EU instruments, at least `Regulation (EU) 2016/679`. Rare here, so a single pattern is enough, but
  do not fold it into the Act grammar.

The notes say around 60 legislation references. The real count of title-bearing mentions is higher,
so report the derived number in the eval rather than repeating theirs.

LLM residue calls present the top 5 candidates together and must accept `NONE` as an answer, and a
`NONE` stays status unresolved with the candidates kept.

### 2.3 Term records (stage 4 output)

Definition site: `{"term": "Buyer System", "definition_node_id": "...", "source": "js1 | inline", "pointer": "optional, when the definition delegates, e.g. to Schedule 6"}`. Use: `{"term": "...", "node_id": "...", "char_span": [..], "status": "confident | ambiguous", "method": "exact_longest | llm | human", "position": "body | heading | sentence_initial"}`.

Matching rules. Case sensitive, longest match wins, overlaps forbidden. Heading or sentence initial position forces status ambiguous. Discovery precision and recall against the given JS1 list is computed in stage 8, so keep discovered and given lists separate.

### 2.4 Concept records (stage 5), Summary records (stage 6)

Concept: `{"id", "label", "scope_path", "member_node_ids": [...], "relations": [{"src", "label", "dst"}], "llm_derived": true, "confidence"}`. After extraction, resolve near duplicates by embedding cosine over the local sentence transformer, threshold in config, merge log kept.

Embeddings are written as `{"node_id", "level", "text", "vector_ref"}` where `level` is one of
`leaf_text`, `subtree_text` or `summary`. Leaves get `leaf_text` only. Containers get
`subtree_text` when the concatenation is under the token budget in `config.py`, and `summary`
always. Summaries are generated text, so they carry `llm_derived: true` and a retrieval hit on a
`summary` vector must resolve to a citable leaf before anything is quoted from it. Vectors are
written to the index keyed by node id, never onto graph nodes, so re embedding never rewrites the
graph.

### 2.5 Graph load (stage 7)

Nodes and edges as JSONL under `output/<run>/graph/`, then loaded to Neo4j. Labels. `Document`, `DocumentVersion`, `Part`, `Provision`, `Table`, `Cell`, `Reference`, `Term`, `Legislation`, `Concept`. Every node and edge gets `batch_id`. Relationships. `HAS_VERSION`, `HAS_PART`, `CONTAINS`, `NEXT`, `HAS_REFERENCE`, `RESOLVES_TO`, `CANDIDATE {score}`, `DEFINED_IN`, `USES_TERM {span, status}`, `ABOUT`, `CONCEPT_REL {label}`, `SUPERSEDES` (DocumentVersion to DocumentVersion) and `PREVIOUS_VERSION` (Provision instance to Provision instance, same lineage_key), both schema only tonight. Constraints before data. Uniqueness on `Provision.id`, `Reference.id`, `Term.name`, `Legislation.key`, `(DocumentVersion.version_id)`, plus a non unique index on `Provision.lineage_key`. MERGE only, never CREATE on possibly existing keys, and every relationship MERGE needs an explicit key or a rerun grows parallel edges. Two functions must exist and be tested. `rollback(batch_id)` removes a batch completely, and `sweep(scope, batch_id)` deletes anything in that scope carrying an earlier batch tag that this batch did not re assert, which is what makes a rerun converge on state rather than only avoiding duplicates. The NetworkX JSON export is written by the same module so graph content has one producer.

### 2.6 Eval report (stage 8)

One JSON plus one human readable markdown per run. Sections. `invariants` (list of checks, pass or fail with locations), `oracle_page_map` (derived vs given, per part agreement), `oracle_outline` (per part, agree count, parser_wrong, outline_wrong, both_differ, sampled triage), `oracle_definitions` (discovery precision and recall vs the given JS1 list, plus terms found outside JS1, plus used but never defined), `golden_references` (precision, recall, abstention correctness against `golden/refs.jsonl`), `golden_terms` (same against `golden/terms.jsonl`), `resolution_transitions` (per batch, unresolved to resolved counts), `stratified_audit` (sample drawn, strata, agreement rate). Metrics live in code, thresholds in `config.py`, and the CLI exits 2 if a gate fails.

### 2.7 Hierarchy profile (stage 0 output, config driven)

The structural ladder is configuration, never hardcoded. `config.py` holds a registry of
`hierarchy_profile` entries. The RM6116 profile declares the ordered unit names, the numbering
grammar at each level, the `unit_label` to use per part family, which kinds are citable, and the
regexes that find an interpretation clause. Registering a new document family means adding a
profile entry and a fixture test, never editing the assembler.

Stage 0 assigns a profile and then checks that it fits, writing `{"profile": "...", "fit": {...}}`
into `output/profile.json`. Five signals, and any one of them quarantines the document rather than
ingesting it with a guessed tree.

1. No interpretation clause found, or one found naming units the profile does not declare.
2. Observed numbering patterns not covered by the profile's grammar, above a threshold.
3. Orphan rate, the share of text blocks that attach to no node, above a threshold.
4. Observed nesting depth outside the profile's declared range.
5. Indentation geometry disagreeing with numbering derived depth, above a threshold.

Quarantine means exit code 2, a written `quarantine.json` naming which signal fired with examples
and page numbers, and no load. Thresholds live in `config.py`. Do not add a fallback that parses
anyway, because a confidently wrong hierarchy corrupts every citation built on it.

## 3. Stage CLI contracts

| Stage | Command | Reads | Writes |
|---|---|---|---|
| 0 | `python -m pipeline.profile` | PDF | `output/profile.json` |
| 1 | `python -m pipeline.parse --parts <ids>` | PDF, profile | `output/<run>/layout/<part>.json` (flat blocks) |
| 2 | `python -m pipeline.assemble` | layout | `output/<run>/tree/<part>.json` (DocNodes), `violations.json` |
| 3 | `python -m pipeline.references` | trees | `output/<run>/refs/<part>.json` |
| 4 | `python -m pipeline.vocabulary` | trees | `output/<run>/vocab/*.json` |
| 5 | `python -m pipeline.concepts` | trees | `output/<run>/concepts.json` |
| 6 | `python -m pipeline.summaries` | trees | `output/<run>/summaries/` |
| 7 | `python -m pipeline.load --batch <id>` | all above | Neo4j + `output/<run>/graph/*.jsonl` |
| 8 | `python -m pipeline.eval` | all above + golden/ + given oracles | `output/<run>/eval/report.{json,md}` |

`config.py` defines the four batches. B1 core-terms pp 1 to 22. B2 joint-schedule-1 pp 112 to 139. B3 award-form pp 23 to 30. B4 call-off-schedule-9 pp 340 to 361. Plus `--full-structural` running stages 0 to 2 and the oracle diffs over all 475 pages with no LLM calls. After each batch load, stage 3 re runs over references with status unresolved and stage 8 records the transitions.

## 4. Known document facts workers must honour

- Top level clause headings look like `3. What needs to be delivered`. Body numbering `3.1`, `3.1.2`, then `(a)` to `(j)`. At least one heading number in Core Terms appears detached from its period. Log, do not repair.
- Numbering restarts in every part. Footers differ per part and carry per part printed page counters and template versions (Core Terms `Version: 3.0.11`, schedules `Model Version: vX.Y`). Store both.
- JS1 paragraphs 1.3.8 and 1.3.9 define reference scoping. Implement them as written.
- Vocabulary is inherited, not global. JS1 is the document level interpretation clause, and several
  schedules carry their own local ones. Verified by cue counts, Call-Off Schedule 2 has 10 local
  definitions and 8 "references to" constructions, Call-Off Schedule 9 has 4 and 4, and Joint
  Schedule 11 and Call-Off Schedule 14 each open with "In this Schedule". Framework Schedule 1 has
  none at all. So resolution order for both a term and a unit label is part local first, then
  document level JS1, and a term defined locally shadows the JS1 definition inside that part only.
  Record which one was used on the `DEFINED_IN` edge.
- Numbering is three dotted levels deep at most, `3`, `3.1`, `3.1.2`, with lettered `(a)` and roman
  `(i)` items below, giving four addressable levels. Verified across all 475 pages: zero four level
  and zero five level numbered lines anywhere, 522 lettered items and 82 roman items. Within the
  Core Terms alone the counts are 146 two level, 43 three level, 169 lettered and 1 roman, so do not
  calibrate the grammar on the Core Terms and assume it generalises. Roman items are a real level in
  the schedules.
- The interpretation clause names Clause, Schedule, Part, Paragraph, Annex and Table, and says
  nothing about the lettered and roman items. So `unit_label` comes from the document for the named
  units and from the `hierarchy_profile` for the unnamed ones. Record which source supplied it.
- Closing words after a list, the Akoma Ntoso `wrapUp` and the Parliamentary Counsel "sandwich", do
  not occur in this document. The profile supports them because legislation uses them heavily. Do
  not build detection for them now.
- Depth is ragged, so a node's kind comes from what it does and never from counting dots. Eight
  numbers in Core Terms are bare sub headings that group children and carry no sentence of their
  own, including `3.1 All deliverables`, `3.2 Goods clauses`, `10.1 Contract Period` and
  `10.3 Rectification plan process`. Every one of these gets no `text` and no `intro` child.
- The brief says Core Terms holds clauses 1 to 35. A straightforward heading regex finds 34, so one
  top level heading does not match the obvious pattern. Find it rather than accepting 34, and record
  what made it different in `anomalies`. This is a deliberate check on the parser, not a trick.
- The definitions schedule is a two column layout, quoted term left, definition right, terms wrap across lines mid cell. Parse by box geometry, not line order.
- The Award Form is numbered label and value rows with `[Insert ...]` placeholders and at least one typo (`rFramework`). It is a form, not clauses.
- The embedded outline (498 entries) and the notes' page map are oracles for stage 8 only. Importing them into stages 0 to 7 is a spec violation.
- Named resolver case, the mislabelled cross reference. The pack mostly follows its own stipulation,
  with 35 references of the form "Paragraph 1.x" and 27 of "paragraph N of this Schedule" against
  only 3 of "Clause 1.x". Those 3 are the interesting ones. Read literally under JS1 1.3.8, a
  "Clause 1.2" appearing inside a Schedule points at Core Terms clause 1.2, when the drafter may
  have meant the local paragraph 1.2. Resolve to the stipulated target, set status `ambiguous`, and
  attach both candidates with the reason. Never resolve these silently, and never drop them.
- Call-Off Schedule 9 contains internal `Paragraph N` references, an Annex, and references that leave the part. `Schedule 6 (ICT Services)` style parentheticals disambiguate family.

## 5. Gates and definition of done

A stage is done when. Its CLI runs clean on its batch inputs. Its outputs validate against `schemas.py`. Its unit tests pass. For stages 1 and 2, the invariant report on Core Terms shows zero unexplained violations (explained anomalies are listed, not hidden). For stage 3, golden precision at or above 0.9 on resolved references and zero golden unresolvables wrongly resolved (abstention is scored). For stage 7, node and edge counts reconcile with stage 2 to 4 outputs exactly, and `rollback` removes a test batch completely. The tester runs `pytest` plus the pipeline end to end on B1 before any merge. The reviewer reads the diff adversarially before any merge. No worker merges their own branch.

## 6. UI contracts (after core merges)

Review UI. FastAPI plus one static page. Lists ambiguous and unresolved references and ambiguous term uses. Each row shows the source sentence, a page image crop rendered from the stored bbox via PyMuPDF, the candidates, and approve, pick candidate, or reject. Decisions append to `golden/decisions.jsonl` with reviewer and timestamp, and stage 8 consumes them as labels. No framework, no build step.

Chat. FastAPI plus one static page, streaming. An Anthropic tool loop (model from `config.py`) with exactly these tools over Neo4j. `find_provision(query)` fuzzy over paths, titles and terms. `get_provision(path)` text plus children plus page and bbox. `follow_references(path, direction)` outbound or inbound resolved references. `define(term)` definition text and source. `cite(path)` returns page image crop bytes for the UI. System prompt requires every claim to carry a `[path, page]` citation, and the UI renders the crop when clicked. Read only Cypher, parameterised, no string built queries.
