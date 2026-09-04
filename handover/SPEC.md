# SPEC. Build contract for the RM6116 knowledge graph pipeline

This is the contract the agent fleet builds against. `DESIGN.md` at the repo root holds the reasoning. If this spec and a worker's instinct disagree, the spec wins. If the spec is wrong, stop and report, do not silently diverge. The spec is frozen for workers, not for the orchestrator, Dan's feedback and orchestrator decisions can change it as the build runs, and a change lands here first, then in `pipeline/schemas.py`, then in code.

## 0. Ground rules

- Python 3.12, the venv at `.venv/`. Dependencies already installed: pymupdf, neo4j, networkx, anthropic, openai, fastapi, uvicorn, pydantic, rapidfuzz, pytest, python-dotenv.
- The PDF is at `../technical-assignment-provided-by-whitespace/document/RM6116 - Network Services 3 - Framework Agreement.pdf`. Treat it as read only. Copy nothing out of it into the repo except derived data under `output/`.
- `ANTHROPIC_API_KEY` (Claude calls) and `OPENAI_API_KEY` (embeddings only, text-embedding-3-large) come from the process environment, or from a gitignored `.env` in the repo root loaded via python-dotenv. Never print them, never commit them, never copy them anywhere else.
- Every stage is a CLI, `python -m pipeline.<stage>`, reading and writing JSON under `output/`. Exit code 0 on success, 2 on invariant violations or quarantine (still writes output plus a violations file), 1 on failure.
- Determinism. Stages 0 to 2 and the deterministic half of 3 and 4 must be pure functions of the PDF bytes and config. Same input, same output, byte for byte. No timestamps inside content, no dict ordering leaks. LLM touching steps record model, prompt version and raw responses under `output/<run>/llm_log/`, and replay from that cache when inputs are unchanged, so reruns are stable.
- All LLM calls go through `pipeline/llm.py` (resolver-builder owns it, everyone imports it). It reads the key, sets the model per task from `config.py`, retries with backoff, logs every call, and serves the replay cache.
- Build for visibility. Finish and demonstrate components as they complete, on fixtures where upstream stages are not ready, rather than waiting for the whole pipeline. Dan reviews increments as they land.

## 1. Repository layout and ownership

```
solution/
  CLAUDE.md                orchestration contract (orchestrator owns)
  DESIGN.md  EVALUATION.md diagram/          (Dan and orchestrator own, workers read only)
  handover/                this spec, task briefs, kickoff prompt, logs, review notes
  config.py                slice definitions, model choices, thresholds    (orchestrator)
  pipeline/
    profile.py             Stage 0   parser-builder
    parse/                 Stage 1   parser-builder
    assemble/              Stage 2   parser-builder
    references/            Stage 3   resolver-builder
    load/                  Stage 7   resolver-builder
    vocabulary/            Stage 4   enrichment-builder
    concepts/              Stage 5   enrichment-builder
    embeddings/            Stage 6   enrichment-builder
    eval/                  Stage 8   eval-builder
    llm.py                 shared    resolver-builder
    schemas.py             pydantic models for every contract below (orchestrator, frozen)
  fixtures/                small hand made stage outputs for downstream work (orchestrator)
  golden/                  hand labels, Dan writes, eval-builder consumes. Small and honest,
                           tens of labels, not hundreds, the harness must not assume scale
  output/                  generated, gitignored
  tests/                   eval-builder owns the harness, every builder adds unit tests for
                           their own stages under tests/<stage>/
  review-ui/               ui-builder
  chat/                    ui-builder
  design/                  Claude Design artboards for the two UIs (ui-builder)
  docs/research/           researcher output
```

One worker never edits another worker's files. Shared needs go through the orchestrator. The ui-builder does not wait for the pipeline, it builds against `fixtures/` from the start and swaps to real `output/` when stages land, only the final integration pass depends on real data.

## 2. Data contracts

`pipeline/schemas.py` is the single source of truth and is committed before any worker starts.

### 2.1 Node, one schema for everything that is ink on the page

```json
{
  "id": "sha1(document + version + path), this version's instance",
  "lineage_key": "sha1(document + path), stable across versions",
  "content_hash": "sha1 of normalised own text, text bearing nodes only",
  "path": "core-terms/3/3.1/3.1.2/a",
  "kind": "document | part | heading | preamble | clause | subclause | item | intro | form_row | table | cell | ref",
  "unit_label": "Clause in Core Terms, Paragraph inside a Schedule, from the interpretation clause",
  "unit_label_source": "document | profile",
  "citable": true,
  "label": "3.1.2 or (a) or null",
  "title": "What needs to be delivered, headings and parts only",
  "text": "own words only, see the per kind rules",
  "page_start": 3, "page_end": 3,
  "printed_page": "3, from the part's own footer",
  "bboxes_own": [{"page": 3, "bbox": [72.0, 401.2, 523.4, 445.0]}],
  "bboxes_extent": [{"page": 3, "bbox": [72.0, 401.2, 523.4, 620.5]}],
  "order": 17,
  "children": ["nested Nodes"],
  "anomalies": ["strings, e.g. numbering_gap_after_3.2.9"],
  "batch_id": "B1"
}
```

There is one node schema for everything that is ink on the page, and `kind` does the differentiating. Optional by kind fields are the accepted cost of one schema, one walker, one loader, one id scheme. Validation is a discriminated union in `schemas.py` enforcing this table:

| kind | text | anatomy children | extra fields |
|---|---|---|---|
| document | never | parts | version_label, source_file, source_sha256, file_created, file_author, ingested_at, pipeline_version, page_routes, custodian, access_label (nullable metadata) |
| part | never | tree | title, part_family, template_version (from its footer) |
| heading, preamble | branch or leaf rule | branch or leaf rule | |
| clause, subclause, item | branch or leaf rule | branch or leaf rule | |
| intro | always | never | citable false |
| form_row | never | label and value cells | |
| table | never | cells | n_rows, n_cols |
| cell | always | never | row, col, cell_role (label, value, header), role_confidence |
| ref | pointing words only | never | see 2.2 |

The branch or leaf rule. A node has anatomy children or it has `text`, never both, at any depth, since depth is ragged (2.10 is a leaf, 3.1 is a bare grouping node, 9.1 has a lead in sentence plus lettered children). Where the source gives a container a lead in sentence, emit it as a first child of kind `intro` with `citable: false` and leave the container's `text` null. The full text of a subtree is a derived view produced by walking it in `order`, never stored. Storing it would put the same sentence in the graph at three levels, would make one edit dirty every ancestor's content hash and wreck the version diff, and would make character offsets ambiguous about which node owns them. Retrieval indexes may hold denormalised text at any granularity, the search index is not the graph.

Ref children are the one exception to how children work, and not to the text rule. A ref does not partition its parent's ink, it annotates a character span of it, so a leaf carries its `text` and its ref children at once. Anatomy children and ref children are disjoint by kind, and the branch or leaf rule quantifies over anatomy kinds only.

The leaf level is the deepest unit the document numbers, the lettered or roman sub paragraph. Do not split leaves into sentences. Sentence precision, where a UI wants it, comes from character offsets into a leaf's text.

Identity is pinned in `schemas.py`, one implementation everyone imports. `id = sha1("{document}|{version}|{path}")`, `lineage_key = sha1("{document}|{path}")`, and `content_hash` is sha1 over a key normalisation (NFC, CRLF to LF, per line trailing whitespace stripped, internal whitespace runs collapsed to one space, ends stripped) that exists for the hash only, stored text is never altered. An intro child takes the path segment `intro`. A table cell appends `<row>/<col>` to its table's path; a form row's cells append `label` or `value`. `order` is the node's preorder position within its part in reading order.

Boxes. `bboxes_own` covers the node's own text, `bboxes_extent` covers the node and everything under it, both as one entry per page touched, and overlap between a parent's extent and a child's is expected. Storing both costs eight floats and saves a subtree walk every time a viewer highlights a whole clause.

Geometric invariants, checked in stage 2 and reported in stage 8. These cross check a tree built from numbering against the geometry that built it, so they catch a mis parented node the numbering alone would accept. A child's left edge is at or right of its parent's. A node's own box sits at or above its first child's. Siblings do not overlap vertically on a page and ascend in reading order. A node's extent stays inside its parent's extent. Violations are recorded in `anomalies`, never repaired silently. An anomaly may carry a model proposed reading with a confidence, stored beside the raw text, never replacing it.

Headers, footers and printed page numbers are stripped from `text` but the printed page is kept as a field. Form parts produce `form_row` nodes with label and value cells. Placeholder text like `[Insert name]` is preserved verbatim. A cell's `cell_role` records what it physically is, and `role_confidence` separately records how plausible that role is, so a header reading like a stray note is recorded as the header it is and flagged as unlikely, both facts kept.

### 2.2 The ref kind

A ref is a place where the text of one provision cites another provision, schedule, definition or statute. It is a node of kind `ref` whose parent is the citing provision, with these rules and extra fields:

```json
{
  "kind": "ref",
  "text": "Framework Schedule 4 (Framework Management), the pointing words as written, never the target's content",
  "char_span": [120, 158],
  "group_id": "optional, shared by refs split from one list phrase",
  "ref_kind": "clause | schedule | paragraph | annex | part | definition | legislation | unknown",
  "scope_rule": "js1_1.3.8 | js1_1.3.9 | title_paren | same_part | none",
  "status": "resolved | ambiguous | unresolved | external",
  "target_path": "core-terms/26 or legislation/bribery-act-2010 or null",
  "candidates": [{"path": "...", "score": 0.82, "reason": "..."}],
  "confidence": 0.97,
  "resolver": "grammar | scope | llm | human",
  "citable": false
}
```

Location is the character offsets into the parent's text, and the ref's box is derivable from those, cached on the node for the review UI. Its `path` is the parent's path plus a span suffix, `/ref@<start>-<end>`, so its id is deterministic like every other node's. Stage 3 writes `output/<run>/refs/<part>.json` as a flat `RefsFile`, `{"part", "refs": [ref nodes]}`, because stage 2 trees carry no ref children, stage 7 attaches each ref to its parent by path. A phrase citing several targets ("Clauses 2.10, 9, 14, 15, 27") becomes one ref per cited target, each anchored to its own number's characters, sharing a `group_id`. Ranges expand inclusively, the interpretation clause says series are inclusive. Refs never mint target nodes, a citation to something the corpus does not contain stays unresolved with candidates kept. Precedent, Akoma Ntoso models inline citations as elements of the document tree, so there is one schema and resolution state is just the mutable part of a ref node.

Stage 3 is two separately specified, separately evaluated steps.

**Detection** finds the pointing words with a citation grammar, an anchor unit word (Clause, Paragraph, Schedule, Annex, Part, Section, Act, Regulations) followed by a number and list grammar (numbers, dots, commas, and, to, ranges, optional parenthetical titles). Behind the grammar runs an orphan keyword detector, any unit keyword not covered by a detected span is surfaced for triage as either generic prose use or a missed citation. The fallback ladder is grammar, then orphan scan, then LLM span extraction on orphan sentences only, then the review queue. Anaphoric forms (this Schedule, that Clause) are detected by pattern but resolved only by LLM or human.

**Resolution** applies the scope rules, part local first, then the document level rules from Joint Schedule 1 paragraphs 1.3.8 and 1.3.9. Nearness is a tree walk to the nearest enclosing part, never raw character distance, with proximity kept only as a candidate scoring feature in the ambiguous residue. LLM residue calls present the top 5 candidates together and must accept `NONE`, and a `NONE` keeps status unresolved with candidates kept.

Legislation targets normalise to `{"title", "year", "instrument_kind", "provision"}` with `target_path` like `legislation/bribery-act-2010` or `legislation/patents-act-1977/section/55`. Normalisation mints keys only and never alters stored text. Three shapes occur and all three must parse, verified by counting across the full document:

- Title plus year, 70 Act mentions and 18 Regulations mentions. Parenthesised qualifiers belong to the title, `European Union (Withdrawal) Act 2018`, `Transfer of Undertakings (Protection of Employment) Regulations 2006`. A greedy regex that stops at the first bracket will truncate these.
- A provision pointer into a statute, 22 occurrences, `Sections 55 and 56 of the Patents Act 1977`. These become one ref per section like clause lists, and the section goes in `provision`.
- EU instruments, at least `Regulation (EU) 2016/679`. Rare here, one pattern is enough, but do not fold it into the Act grammar.

Near miss titles are an entity resolution problem, not a text problem. Two mentions whose normalised keys differ but whose character overlap or embedding similarity crosses the thresholds in `config.py` (`European Union (Withdrawal) Act 2018` against a hypothetical `European Union Act 2018`) are routed, LLM first, human if still uncertain, before either mints a separate Legislation node. Stage 8 reports how often routing fired, whether it fired everywhere it should have (seeded test cases), and how the routed decisions scored.

The notes say around 60 legislation references. The real count of title bearing mentions is higher, so report the derived number in the eval rather than repeating theirs.

### 2.3 Term records (stage 4 output)

Term uses are edges, not nodes, their target always exists, which is exactly the property refs lack.

Definition site: `{"term", "definition_node_id", "source": "declared | discovered | both", "scope": "document | part local, with the part", "aliases": ["CCS"], "pointer": "optional, when the definition delegates, e.g. to Schedule 6"}`. Aliases are captured from the parenthetical abbreviation convention at first use, the Crown Commercial Service (CCS), and matched with the same rules as full forms. Definition texts are themselves matched for term uses, and every term used inside another term's definition yields a deterministic `DEFINED_USING` edge between the two Term nodes, the vocabulary's own dependency graph. Stage 8 reports its cycles and maximum chain depth as anomalies.

Use (becomes a `USES_TERM` edge): `{"term", "node_id", "char_span", "status": "confident | ambiguous", "ambiguity_kind": "none | sentence_initial | heading | typo_dense | alias_collision", "method": "exact_longest | llm | human", "definition_used": "which definition site, since local definitions shadow JS1 inside their part"}`. `definition_used` holds the governing site's scope string, `document` or `part:<part-id>`, which with the term names the site uniquely. `char_span` offsets into the node's `text`, or into its `title` for heading matches, which is what the heading ambiguity kind marks.

Matching rules. Case sensitive, longest match wins, overlaps forbidden, aliases equal to full forms. Sentence initial or heading position forces status ambiguous. A deterministic per section typo density signal (spelling and obvious grammar checks) forces `typo_dense` on matches from high typo sections, because there a capital letter may be an accident and a missing one may hide a real use. Different `ambiguity_kind` values route to different narrow LLM prompts, each written for its failure mode. A stratified random sample of confident matches is also routed for audit, strata and sample size in `config.py`. Keep declared and discovered lists separate so stage 8 can diff them.

### 2.4 Concept records (stage 5), embeddings (stage 6)

Concept: `{"id", "label", "scope_path", "member_node_ids", "relations": [{"src", "label", "dst"}], "llm_derived": true, "confidence"}`. The scan unit is a part or top level clause with its full derived subtree text, long enough context to see how language is actually used. After extraction, resolve near duplicates by embedding cosine (text-embedding-3-large, like all embeddings in this build), threshold in `config.py`, merge log kept. A proposed concept whose label collides with a declared Term, exact, alias, or embedding near duplicate above the same threshold, is not minted, log the collision instead, tier 2 outranks tier 3. After resolution, compute `ASSOCIATED_TERM` edges, concept to term, deterministically, for each concept the terms its member provisions use, weighted by the share of member provisions using the term, kept above `ASSOCIATED_TERM_MIN_SHARE` in `config.py`. This aggregation joins stage 4 and stage 5 outputs, and stages 3 to 6 never read each other's output, so it runs inside stage 7, the join, owned by resolver-builder. The aggregation is deterministic but its inputs include generated tags, so the edge carries `llm_derived: true` and never enters a citation path.

Embeddings are written as `{"node_id", "level", "text", "vector_ref"}` where `level` is `leaf_text`, `subtree_text` or `summary`. Leaves get `leaf_text`. Containers get `subtree_text` when the concatenation fits the token budget in `config.py`, and `summary` for whole documents, parts, and containers too long to embed directly. Summaries are generated text, they carry `llm_derived: true`, and a retrieval hit on a summary vector must resolve to a citable leaf before anything is quoted. Vectors are written to the index keyed by node id, never onto graph nodes, so re embedding never rewrites the graph. A `leaf_window` variant, the leaf embedded with its previous and next sibling as context, is implemented behind a config flag as a replacement for `leaf_text`, off by default, for the A B retrieval comparison described in `EVALUATION.md`. Never store both variants for the same leaf.

Confidence, everywhere it appears. For deterministic resolvers, confidence is not asserted by the code, it is the empirically measured precision of that resolver class from stage 8, attached at load time. For LLM steps, the model must emit its score in the same structured response as its ranked candidates, scored before it commits to a final answer so it is not defending a conclusion it already stated, and stage 8 calibrates those raw scores against the golden labels per resolver, reporting a reliability table. A confidence that has never met ground truth is a vibe, not a number.

### 2.5 Graph load (stage 7)

Nodes and edges as JSONL under `output/<run>/graph/`, then loaded to Neo4j. An edge row is a `GraphEdge` from `schemas.py`, `{"type", "src", "dst", "props", "batch_id"}`, where src and dst are node ids or referent keys (`Term.name`, `Legislation.key`, `Concept.id`), and the MERGE key is type plus endpoints plus the discriminating prop where several edges legally join one pair, `char_span` for `USES_TERM`. Every node carries the `:Node` label plus a secondary label from its kind (`:Clause`, `:Ref`, ...). Referent labels outside the tree: `:Term`, `:Legislation`, `:Concept`. Uniqueness constraints collapse to `Node.id`, plus `Term.name`, `Legislation.key`, `Concept.id`, and a non unique index on `Node.lineage_key` and on `Node.label` (the printed number, for lookup by "9.2").

Edges: `CONTAINS` (the tree, including document to part and provision to ref), `NEXT` (reading order between siblings), `RESOLVES_TO` (ref to target), `CANDIDATE {score}` (ref to candidate), `USES_TERM {char_span, status, ambiguity_kind, definition_used}`, `DEFINED_IN` (term to defining provision), `ABOUT` (provision to concept), `DEFINED_USING` (term to term, deterministic, from term matches inside definition texts), `CONCEPT_REL {label}` (concept to concept, LLM derived), `ASSOCIATED_TERM {share, llm_derived: true}` (concept to term, deterministic aggregation over mixed trust inputs), `SUPERSEDES` (document root to document root, schema only tonight). Version history of one provision is a query over `lineage_key`, not an edge.

MERGE only, never CREATE on possibly existing keys, and every relationship MERGE needs an explicit key or a rerun grows parallel edges. Every node and edge gets `batch_id`. Three functions must exist and be tested. `rollback(batch_id)` removes a batch completely. `sweep(scope, batch_id)` deletes anything in scope carrying an earlier batch tag this batch did not re assert, which is what makes a rerun converge on state rather than only avoiding duplicates. `salience()` recomputes the salience property for structural nodes and terms, breadth times log damped frequency as specified in `DESIGN.md`, constants in `config.py`. Merges, sweeps, rollbacks and dedups append to an audit log with batch, affected ids and reason. The NetworkX JSON export is written by the same module so graph content has one producer. Operational note for the design surface, not tonight's build: production runs identical replicas for staging and testing, and per batch snapshots stored on separate infrastructure.

### 2.6 Eval report (stage 8)

One JSON plus one human readable markdown per run. Sections, named so they explain themselves:

- `invariants`: structural and geometric checks, pass or fail with locations.
- `page_map_vs_provided`: the derived page map diffed against the one in the assignment notes, per part agreement, plus the derived part count against their stated 46 and their table's 48 rows.
- `outline_vs_provided`: the derived tree diffed against the PDF's embedded 498 entry outline, per part, agree count, parser_wrong, outline_wrong, both_differ, from a sampled triage.
- `definitions_vs_provided`: discovery precision and recall against the declared JS1 list, terms discovered outside JS1, capitalised phrases used but never defined.
- `golden_refs`: detection recall and resolution precision reported as separate numbers, plus abstention correctness, no golden unresolvable resolved.
- `golden_terms`: same shape for term uses, with the FP and FN counts reported per ambiguity kind and a cost weighted summary, weights in `config.py` as placeholders a domain expert would set.
- `stratified_audit`: sample drawn, strata, agreement rate, disagreements listed.
- `confidence_calibration`: reliability table per resolver, raw score bucket against observed precision.
- `resolution_transitions`: per batch, unresolved to resolved counts.
- `concepts`: duplicate rate after resolution, coverage (sections with zero concepts), spot check sample for human eyes.

Metrics live in code, thresholds in `config.py`, and the CLI exits 2 if a gate fails. Scope control: by default the report covers the parts touched by the batch, `--full` runs the whole battery over everything, which is the scheduled sweep mode.

### 2.7 Hierarchy profile, the rulebook (stage 0 output, config driven)

A hierarchy profile is a rulebook describing how a family of documents is structured, stored as config, the ordered unit names, the numbering grammar at each level, which units are citable, which kinds can occur, the `unit_label` per part family, and the patterns that find an interpretation clause. The assembler stays a generic machine that reads a rulebook. When a new family shows up, an EU regulation with Articles and Recitals, a US contract with Section 1.01, you add a rulebook entry and a fixture test. You never crack open the parser, which is where regressions come from.

Stage 0 assigns a rulebook and then checks the shoe actually fits, writing `{"profile", "fit"}` into `output/profile.json`. Five fit checks, in plain terms:

1. No interpretation clause found, or one found naming units the rulebook has never heard of. Wrong rulebook.
2. Too much numbering matching none of the rulebook's patterns. Roman numerals at the top level, say. Wrong rulebook.
3. Too much homeless text. After building the tree, what fraction of the text attached to no node. If lots of text has no home, the tree does not really describe this document.
4. Depth out of range. Rulebook says at most four levels, document shows six.
5. Indentation disagrees with numbering. In a sane parse, 3.1.2 sits further right on the page than 3.1. If numbering says one depth and layout says another, the tree is probably mis built even though the numbers looked fine.

Any alarm quarantines the document, exit code 2, a written `quarantine.json` naming the signal with examples and page numbers, and no load. Thresholds live in `config.py`. Do not add a fallback that parses anyway, a confidently wrong hierarchy corrupts every citation built on it.

Multi-template bindings get the same checks at the granularity the pack actually has. When profiling detects that one file is a binding of separately versioned templates, each with its own footer signature, the five fit checks also run per part: parts that pass proceed, parts that fail quarantine individually with their evidence in `fit_by_part`, and stage 1 refuses a quarantined part with no override flag. The document level verdict is still computed and reported. This is not a relaxation, it is the fit check applied to the thing that actually has a house style, and a binding whose parts disagree about numbering conventions is exactly the case the per document number would misjudge in both directions.

## 3. Stage CLI contracts

| Stage | Command | Reads | Writes | Runs |
|---|---|---|---|---|
| 0 | `python -m pipeline.profile` | PDF | `output/profile.json` | first |
| 1 | `python -m pipeline.parse --parts <ids>` | PDF, profile | `output/<run>/layout/<part>.json` | per part, parallel |
| 2 | `python -m pipeline.assemble` | layout | `output/<run>/tree/<part>.json`, `violations.json` | per part, parallel |
| 3 | `python -m pipeline.references` | trees | `output/<run>/refs/<part>.json` | parallel with 4, 5, 6 |
| 4 | `python -m pipeline.vocabulary` | trees | `output/<run>/vocab/*.json` | parallel with 3, 5, 6 |
| 5 | `python -m pipeline.concepts` | trees | `output/<run>/concepts.json` | parallel with 3, 4, 6 |
| 6 | `python -m pipeline.embeddings` | trees | `output/<run>/embeddings/` | parallel with 3, 4, 5 |
| 7 | `python -m pipeline.load --batch <id>` | all above | Neo4j + `output/<run>/graph/*.jsonl` | join |
| 8 | `python -m pipeline.eval [--full]` | all above + golden/ + provided artifacts | `output/<run>/eval/report.{json,md}` | after 7 |

Parts fan out from stage 1, stages 3 to 6 share the trees and none reads another's output, so after assembly they all run concurrently, load is the join, eval follows load. `config.py` defines the four batches. B1 core-terms pp 1 to 22. B2 joint-schedule-1 pp 112 to 139. B3 award-form pp 23 to 30. B4 call-off-schedule-9 pp 340 to 361. If the build compresses, B1 and B3 are the must haves. Plus `--full-structural`, stages 0 to 2 and the structural cross checks over all 475 pages with no LLM calls. After each batch load, stage 3 re runs over refs with status unresolved and stage 8 records the transitions.

## 4. Document family rulebook, and this document's quirks

Two lists, kept apart deliberately. The first is the rulebook content for the uk-ccs-framework family, generalisation happens by adding rulebooks, not by editing parsers. The second is quirks of this specific pack, which the prototype must survive and the tests pin down, and which prove the anomaly machinery works rather than defining the system.

Rulebook entries (config):

- Top level headings shaped `3. Title`, body numbering `3.1`, `3.1.2`, lettered `(a)` and roman `(i)` items below, four addressable levels, no dotted numbering deeper than three levels.
- Unit labels from the interpretation clause where it names them (Clause, Schedule, Part, Paragraph, Annex, Table), from the rulebook for the ones it is silent on (lettered and roman items). Record which source supplied each label.
- Interpretation clause cues, "unless the context otherwise requires", "In this Schedule", "references to". Vocabulary is inherited, part local definitions shadow document level ones inside their part, resolution order part local first then JS1, record which definition was used.
- Reference scoping per JS1 1.3.8 and 1.3.9, series inclusive per 1.3.10.
- Numbering restarts per part. Footers carry per part printed page counters and template versions, store both.
- Closing words after a list (the legislative sandwich) are supported by the rulebook and unused by this document, do not build detection now.

Quirks of this pack (tests and anomalies, not design inputs):

- Verified counts across all 475 pages: zero four level dotted numbers anywhere, 522 lettered items, 82 roman items. Within Core Terms alone, 144 two level, 43 three level, 169 lettered, 1 roman, so do not calibrate the grammar on Core Terms and assume it generalises, roman items are a real level in the schedules. The earlier count of 146 two level provisions included two cross references that start a wrapped line ("...10.4.3, 10.5 or 20.2 or a Contract expires..."), which the parser records as `numbering_read_as_wrapped_text` rather than minting phantom provisions.
- Depth is ragged. Eight numbers in Core Terms are bare sub headings grouping children with no sentence of their own, including 3.1, 3.2, 10.1 and 10.3. These get no text and no intro child.
- The brief says Core Terms holds clauses 1 to 35, and all 35 parse: the earlier claim here that a straightforward regex finds only 34 did not survive the parser, Core Terms headings are typographically identical. The real detached-number case is Framework Schedule 5, whose second top level heading prints "2   Reporting period" with no period after the number. It is recovered from the part's own heading typography and sequence position, with `heading_number_missing_period` recorded, and the behaviour is pinned in `tests/parse/test_heading_quirk.py`. The lesson stands: find the non conforming heading, record what made it different, never guess.
- The definitions schedule is a two column layout, quoted term left, definition right, terms wrap across lines mid cell. Parse by box geometry, not line order.
- The Award Form is numbered label and value rows with `[Insert ...]` placeholders and at least one stray character typo (`rFramework`). It is a form, not clauses. Log, never repair.
- The mislabelled cross reference case. The pack mostly follows its own stipulation, 35 "Paragraph 1.x" and 27 "paragraph N of this Schedule" against 3 "Clause 1.x" inside Schedules. Resolve those 3 to the stipulated target, set status ambiguous, attach both candidates with the reason. Never silently, never dropped.
- Call-Off Schedule 9 contains internal Paragraph references, an Annex, and refs that leave the part. `Schedule 6 (ICT Services)` style parentheticals disambiguate family.
- The embedded outline (498 entries) and the notes' page map are stage 8 cross checks only. Importing them into stages 0 to 7 is a spec violation.

## 5. Gates and definition of done

A stage is done when. Its CLI runs clean on its batch inputs. Its outputs validate against `schemas.py`. Its unit tests pass. For stages 1 and 2, the invariant report on Core Terms shows zero unexplained violations (explained anomalies are listed, not hidden). For stage 3, on the golden set, detection recall at or above 0.95, resolution precision at or above 0.9 on resolved refs, and zero golden unresolvables wrongly resolved, abstention is scored. For stage 7, node and edge counts reconcile with stage 2 to 6 outputs exactly, and `rollback` plus `sweep` remove and converge a test batch completely. The tester runs `pytest` plus the pipeline end to end on B1 before any merge. The reviewer reads the diff adversarially before any merge. No worker merges their own branch. Golden thresholds are computed over a deliberately small honest label set, the harness reports absolute counts alongside rates so nobody mistakes 9 of 10 for 900 of 1000.

## 6. UI contracts

Both UIs start immediately against `fixtures/`, not after the pipeline. Design before code, using Claude Design (the `/design` skill, not an MCP), with artboards written under `design/` rather than scattered through the repo. For each key screen, put two or three visual directions on one canvas first, choose one, and record in a sentence why. From then on the approved artboard is the source of truth, build components to match it, and if implementation forces a change, update the artboard so the two never drift. Reserve the canvas for the screens where the look carries the product, the review queue row and the chat answer with its citation, routine views go straight to code. Publish each canvas as an Artifact and put the URL in progress notes so Dan can review without interrupting.

**Review UI.** FastAPI plus one static page. Lists ambiguous and unresolved refs, ambiguous term uses, and anomalies with proposed readings. Each row shows the source sentence, a page image crop rendered from the stored box via PyMuPDF, the candidates, and per kind verdict controls that map one to one onto the golden vocabulary below. Decisions append to `golden/decisions.jsonl` with reviewer and timestamp, and stage 8 consumes them as labels.

The decisions record is the contract seam between the two surfaces, so its vocabulary is pinned here and detailed in `pipeline/eval/GOLDEN_FORMAT.md`, which elaborates, never contradicts. Ref verdicts: `target` (the pipeline's target or a picked candidate is right, `chosen_candidate` required), `unresolvable` (a real citation with no target in the corpus, the label the abstention gate feeds on, and the UI must make it reachable), `not_a_reference` (not a citation at all). Term verdicts: `use` (`chosen_candidate` names the governing term, which may differ from the matched one in alias collisions) and `not_a_use`. Anomaly verdicts: `confirmed`, `rejected`, plus the triage set `agree`, `parser_wrong`, `outline_wrong`, `both_differ` for derived versus provided rows. Node anomaly records carry an `anomaly_index` so two anomalies on one node hold separate verdicts; triage rows are keyed by their queue id instead, there being no node anomaly to index. Demo and test flows write only to temporary paths, so a `decisions.jsonl` in the repo always holds real reviewer verdicts.

**Chat.** FastAPI plus one static page, streaming. Three pieces, kept deliberately small.

- A gate. A cheap fail open classifier decides plain lookup or research, anything ambiguous defaults to research.
- A disambiguation and planning step. Before any tool runs, the model restates the question as a set of focused sub queries with an explicit parallel batch structure, which is also what gets shown to the user as "working on".
- A bounded tool loop over Neo4j with exactly these tools. `find_provision(query)` fuzzy plus embedding entry points over paths, titles, terms and summaries. `get_provision(path)` derived text plus children plus page and box. `follow_references(path, direction)` outbound or inbound resolved refs. `define(term)` definition text, source, and which definition governs in that part. `find_by_concept(label)` tier 3 narrowing. `history(lineage_key)` version chain, empty tonight but wired. `cite(path)` page image crop bytes.

Answers must attach `[path, page]` citations to every claim, citations come only from tool output, never invented, and the UI renders the crop when clicked. Read only Cypher, parameterised, no string built queries. Embeddings come from text-embedding-3-large via `pipeline/embeddings`, batched and cached under `output/` so repeat runs cost nothing.
