# Design. RM6116 into a knowledge graph

Dan Porder, 3 September 2026. This document is the reasoning behind the diagram in `diagram/`. The evaluation approach has its own document, `EVALUATION.md`. The material I handed to the coding agents is in `handover/`.

## 1. What the graph is for

The shape of the questions decides what is worth extracting, so I started there. Five shapes cover most of what a system like this gets asked.

1. Point lookup with proof. "What does Clause 9.2 say", answered with the text, the page and the exact region on the page.
2. Obligation questions. "What must the Supplier do if a Default occurs", which needs the clause tree, the defined terms, and the references between them.
3. Impact questions. "What depends on Joint Schedule 11", which is the reverse closure over references, and is what an amendment assessment runs on.
4. Definition scoping. "What does Buyer System mean in this clause", which depends on where the definition lives and which schedule you are standing in.
5. Corpus questions, once there are thousands of documents. "Which of our frameworks cite the Bribery Act 2010", which only works if legislation references are normalised nodes rather than strings.

Everything below is judged against those five. Anything that serves none of them was cut.

## 2. The data model, three tiers

The graph has three tiers with different trust levels and different costs. Keeping them separate is the core design decision, because it means a cheap deterministic layer is never contaminated by a probabilistic one, and every edge knows which kind it is.

### Tier 1, structure. Deterministic.

The document tree. A document has parts, parts have sections and clauses, clauses have sub clauses and lettered sub paragraphs, and every one of those is its own node with its own page range and its own bounding boxes. Nothing is glued into a parent's text blob.

#### Where the node types come from

I did not want this ladder to be my invention, so it comes from three sources that agree with each other.

The first is what Whitespace described to me, which was document has section, section has clause, clause has sub clause, sub clause references other clause. That is the shape to preserve, and it was offered as a sketch rather than a schema, so I have kept its skeleton and extended it where this document needs more.

The second is the document itself, which declares its own structural vocabulary in its interpretation clause. Joint Schedule 1, paragraph 1.3.8, says that references to Clauses and Schedules mean the clauses and schedules of the Core Terms, and that references in any Schedule to parts, paragraphs, annexes and tables mean those of the Schedule they appear in. Paragraph 1.3.9 says Paragraphs means the paragraph of the appropriate Schedule. So the document names its own units, Clause, Schedule, Part, Paragraph, Annex and Table, and it tells you which name applies where. Deriving the vocabulary from the interpretation clause rather than hardcoding it is the difference between a parser that works on this document and one that works on the family.

Vocabulary is inherited rather than global, which I checked rather than assumed. Joint Schedule 1 is the document level interpretation clause, and several schedules carry their own local ones on top of it. Call-Off Schedule 2 defines ten terms of its own, Call-Off Schedule 9 defines four, and Joint Schedule 11 and Call-Off Schedule 14 both open with "In this Schedule". Framework Schedule 1 has none at all and relies entirely on the document level. So resolution runs part local first and document level second, a locally defined term shadows the general one inside that part only, and the graph records which definition was used on each edge. A system that treated the definitions schedule as the single global dictionary would quietly return the wrong meaning inside those schedules.

That produces a fact worth building on. The same structural role carries a different label depending on which part you are standing in. Provision 9.2 of the Core Terms is a Clause. The provision with the same shape inside Call-Off Schedule 9 is a Paragraph. So there is one `Provision` node type with a `unit_label` property resolved per part, which means a citation renders as "Clause 9.2" in one place and "Paragraph 3.2" in another without a special case in the renderer. Getting that wrong is the kind of thing a lawyer notices immediately and an engineer never does.

The drafters do not follow their own rule perfectly, and the exceptions are where this stops being tidy. Across the pack there are 35 references of the form "Paragraph 1.x" and 27 of "paragraph N of this Schedule", against 3 that say "Clause 1.x" while sitting inside a Schedule. Those 3 are the whole problem in miniature. Read literally under paragraph 1.3.8, a reference to "Clause 1.2" points at Core Terms clause 1.2, but a drafter writing it inside a schedule may well have meant the local paragraph 1.2. There is no way to tell from the text alone. A resolver that applies the stipulated rule confidently gets them wrong and reports nothing, which is the exact failure the brief describes as worse than failing loudly. So they resolve to the stipulated target, carry status ambiguous with both candidates and the reason attached, and go to review. Three references out of roughly fifteen hundred is a rounding error until one of them is the clause someone is relying on.

The third is the observed numbering, which decides depth. Numbering runs to three dotted levels with lettered items below, and the depth is ragged. Clause 9.1 carries text and has lettered children, while 3.1 is a bare sub heading with no text of its own that exists only to group 3.1.1 and 3.1.2. That is why a node's kind is decided by what it does, holding text or holding children, rather than by counting dots in its number.

The floor of the ladder is the lettered or roman sub paragraph, because that is the deepest unit the document numbers and therefore the deepest unit anyone can cite. Across the pack there are 522 lettered and 82 roman items, and no numbering deeper than three dotted levels, so four addressable levels covers it.

Sentences are not a unit here, and I checked that rather than assuming it. Akoma Ntoso, the OASIS standard for legal documents, has no sentence element at all across its 315 element declarations. UK citation practice has no numbered form for one. The clearest evidence is a Court of Appeal judgment, Al Mana Lifestyle Trading v United Fidelity Insurance [2023] EWCA Civ 61, where the parties had to add their own bracketed numbers to a clause for argument, because the sentences inside it carried no identifier of their own. Splitting sentences would also be probabilistic on legal prose, which is dense with abbreviations and semicolon separated limbs, so it would put a guessed boundary inside the one tier that is meant to be deterministic. Where a UI wants to highlight a sentence, that is a character offset into a leaf, which is the mechanism references and term mentions already use.

Two things corroborate the branch or leaf rule from earlier. Akoma Ntoso models a container as an exclusive choice, either an optional intro, then children, then an optional wrap up, or a single content block, never both. The Office of the Parliamentary Counsel drafting guidance describes the same shape as a sandwich of opening words, paragraphs and closing words. Landing on that independently and then finding it twice attested is reassuring. RM6116 itself never uses closing words, so the profile supports them and the parser does not look for them.

One limit worth stating. The interpretation clause names Clause, Schedule, Part, Paragraph, Annex and Table, and says nothing at all about the lettered and roman items, which is most of the leaves. So vocabulary comes from the document for the units it names and from the profile for the ones it does not, and the graph records which source supplied each label. Deriving vocabulary from the document is the right default and it does not cover everything.

#### When a document does not fit the ladder

The ladder above is right for a UK public sector framework agreement and wrong for plenty of other things. A US style contract has Articles and Recitals. A technical manual has none of this. So the hierarchy is configuration rather than code. A `hierarchy_profile` declares the ordered unit names, the numbering grammar at each level, which units are citable, and the interpretation clause patterns to look for. Adding a document family is a config entry and a fixture test, not a parser rewrite.

The profiling stage then checks that the assigned profile actually fits, and refuses rather than guesses when it does not. Five signals, any of which quarantines the document with its evidence attached instead of ingesting it. No interpretation clause is found, or one is found naming units the profile does not have. The observed numbering does not match the profile's grammar. Too high a share of text blocks fail to attach to any node, which means the tree is wrong even where it parsed. Nesting depth falls outside the profile's expected range. Or the indentation geometry disagrees with the numbering above a threshold.

A quarantined document goes to a person with the specific mismatch shown, so the conversation is about which profile it needs rather than about why the graph looks strange. This is the one place I would rather fail loudly than proceed, since a confidently wrong hierarchy silently corrupts every citation built on top of it.

Nodes. `Document`, `DocumentVersion`, `Part`, `Provision` (one node type for section, clause, sub clause and lettered sub paragraph, distinguished by a `kind` property and labelled per part by `unit_label`), `Table`, `Cell`, `Reference`.

Edges. `HAS_VERSION`, `HAS_PART`, `CONTAINS` (the tree), `NEXT` (reading order between siblings), `HAS_REFERENCE`, `RESOLVES_TO`, `CANDIDATE`.

References are nodes, not edges. A `Reference` carries the raw text, the kind (clause, schedule, paragraph, annex, definition, legislation), the scope rule that was applied, a status of resolved, ambiguous, unresolved or external, a confidence, the candidates it could not choose between, and which resolver produced it. An unresolved reference is a first class citizen of the graph. It never silently disappears, it is what the review queue is made of, and it is what flips to resolved when the target document is ingested later.

The document supplies its own scoping rules. Joint Schedule 1, paragraphs 1.3.8 and 1.3.9, state that "Clause" means the Core Terms and that "Paragraph" means the schedule you are currently standing in. The resolver implements those two paragraphs directly. When a bare "Schedule 6" appears, the parenthetical title, as in "Schedule 6 (ICT Services)", is used to disambiguate between the three schedule families, and when that fails the reference is marked ambiguous with its candidates attached rather than guessed.

Legislation references become `Legislation` nodes keyed on normalised title, year and, where the citation gives one, the provision inside the statute. Counting them across the document turns up 70 Act mentions, 18 Regulations mentions and 22 that point at a specific section, such as Sections 55 and 56 of the Patents Act 1977. So a legislation reference is not always a reference to a whole statute, and modelling it as one would lose the pointer that actually matters. Titles also carry parenthesised qualifiers that belong to the title, as in the European Union (Withdrawal) Act 2018, which is the sort of thing a normaliser truncates if nobody looked.

In this prototype that is where it stops. In production I would resolve these against the legislation.gov.uk identifier scheme, which addresses provisions directly, or a mirrored index of it inside an air gapped deployment, so that a claim an agent makes can be traced from answer to clause to the exact section of the exact statute. That chain is the thing a regulated client asks about first, and it only exists if legislation is a normalised node rather than a string sitting in some text.

### Tier 2, vocabulary. Discovered by rule, matched deterministically.

Joint Schedule 1 defines roughly 300 capitalised terms, and the schedules define more inline. Each becomes a `Term` node with a `DEFINED_IN` edge to the provision that defines it, and every use in the text becomes a `USES_TERM` edge with the character span. Parties and roles, Supplier, Buyer, CCS, Relevant Authority, are terms, so this tier is also what gives the graph its actors.

The interesting part is that the vocabulary is discovered rather than given. Legal drafting marks definitions by convention, a quoted capitalised phrase followed by "means" or "has the meaning", or the parenthetical form. The pipeline extracts definition sites by that rule, then compares its discovered list against the provided definitions schedule as an evaluation. Terms discovered outside Joint Schedule 1, such as terms defined inline within a call off schedule, are wins a fixed list would have missed. Capitalised phrases that are used but never defined are an anomaly signal worth a human's eyes, since each one is either a drafting error or a discovery miss. This is what makes the tier scale to documents that do not ship with a definitions schedule, and the honest caveat is that the convention is a drafting house style. I would test the rule on other CCS frameworks first, where it should transfer, and then on a non CCS contract to find where it breaks.

Matching uses the term list with three rules. Case sensitive exact match, longest match wins so Call-Off Contract beats Contract, and sentence initial or heading position marks the match ambiguous rather than confident, because that is where a capital letter stops being evidence. Ambiguous matches are routed to an LLM check and to the review queue. A stratified random sample of the confident matches is routed as well, stratified by term length, document part and match position, because auditing only the self declared hard cases would let a systematic error in the easy cases run silently.

This matcher is the bootstrap. The scale path is a small local NER model fine tuned from the matcher's confirmed output plus human corrections, which removes the brittleness of casing conventions entirely. Selecting which model family to test, GLiNER style zero shot span models against fine tuned token classifiers, is a research task I have scoped into the agent workstream rather than guessed at here.

### Tier 3, concepts. LLM derived, flagged as such.

An LLM scans each part and top level clause and returns the key concepts it is about, and the relationships between those concepts. Every provision in that scope gets an `ABOUT` edge to its concepts, and concept to concept edges form a small side ontology. Concepts are resolved with embedding similarity and clustering so that near duplicates collapse, the same move as entity resolution in my previous graph work. Every concept node carries an `llm_derived` flag and a confidence.

The rule that keeps this tier honest is that concepts are navigation, never citation. An answer cites provisions, pages and boxes from tier 1. Concepts only help an agent or a human find the right neighbourhood. This is also why the tier can afford to be probabilistic while the tiers below it are not.

### What is deliberately not modelled

- Obligations, rights and permissions as typed nodes. The next semantic type I would add, extracted per provision with obligor, action and condition, because question shape 2 wants it. It needs its own evaluation set and did not fit the time cap.
- Temporal validity and point in time queries. The versioning design below makes it possible later. Modelling it now would double the schema for no answerable question tonight.
- Precedence and conflict between clauses. Real, hard, and dishonest to fake with a heuristic.
- Anything about a specific call off contract. This document is the framework template. The model reserves room for executed call offs as sibling documents referencing it.

## 3. Identity, provenance and idempotency

A node's identity is document, version, part, then path, for example `rm6116/v3.0.11/core-terms/3/3.1/3.1.2/a`. Never the bare clause number, because numbering restarts in every part, and the same string "paragraph 2.2" names different things in different schedules.

Two keys come off that path. The `lineage_key` is a SHA-1 of the document and path with the version left out, so it names clause 3.1.2(a) of RM6116 as a thing that persists across amendments. The node `id` is a SHA-1 of the full path including the version, so it names this version's instance of that clause, holding that version's text, pages and boxes. Instances sharing a lineage key chain together with `PREVIOUS_VERSION` edges, and each instance stores a hash of its own normalised text, so a version diff reports changed, unchanged, added and removed without re reading the PDF. Hashing rather than using the path string directly is convenience rather than correctness. Paths carry slashes, spaces and parentheses that need escaping everywhere they travel, and a fixed width key indexes better. The readable path stays on the node as a property, because that is what a person wants to see in a query result.

Deterministic ids are what make re ingestion safe. The loader uses MERGE against a uniqueness constraint on `id`, so a second run over the same input finds the existing node and updates its properties instead of creating a twin. That is what upsert means here, update if present and insert if absent, and it is the thing that prevents duplication rather than causing it. Ids minted at ingestion time, a UUID or a counter, would create a second node for the same clause on every run and silently double the graph.

Upsert on its own is not the whole story, and three gaps are worth naming. A node the first run created and the second run no longer asserts would otherwise sit there orphaned, so each load is scoped and swept, deleting anything under that scope carrying the previous batch tag that this batch did not re assert. Relationship merges need their own keys or a rerun grows parallel edges between the same pair. And the generative stages are not deterministic, so their ids stay stable while their confidences and candidate lists drift between runs, which is why every model call is logged and replayable from a cache keyed on its input. The deterministic stages are idempotent by construction. The LLM stages are idempotent only if you pin them.

Every provision carries the absolute PDF page range, the printed page number from the part's own footer, which restarts per part and is what a lawyer would quote, and two sets of bounding boxes. One covers the provision's own text, the other covers the provision and everything nested under it, each as one box per page touched. Overlap between a parent's extent and its children is expected, and it is useful, because it lets a viewer highlight either a whole clause or one lettered sub paragraph without walking the tree first. Every claim downstream can therefore be proven by rendering the page and drawing the box.

Text does not work the same way. Raw text lives only on leaf nodes, and the full text of a clause plus its sub paragraphs is a derived view rather than a stored field. Three things break if text is copied up the tree. The same sentence lands in the graph at three levels, so a vector search returns the same passage as three different hits and a citation cannot say which level actually matched. A change to one lettered paragraph dirties the content hash of every ancestor, so the version diff reports a whole clause as amended when one line moved. And references and term uses are stored as character offsets into a node's text, which stop being unambiguous once the same sentence exists in several nodes. The search index is free to hold denormalised text at whatever granularity retrieval wants, because the index is not the graph.

So the rule is that a node has children or it has text, and never both. Where the source gives a clause a lead in sentence and then sub paragraphs, as clause 9.1 does with its "to enable it to both" followed by (a) and (b), the lead in becomes its own child of kind `intro`, marked as not independently citable. Text then lives only on leaves, and every container is pure structure. The legislative XML standards in the Akoma Ntoso family landed on the same shape for the same reason, which is some comfort that it is not an idiosyncratic choice. The cost is one extra node per container that has a lead in, and one node type that has no legal identity of its own.

The bottom of that hierarchy is the deepest unit the document itself numbers, which here is the lettered sub paragraph. I considered going further and making sentences the leaves, and decided against it. Sentence splitting is probabilistic on legal text, which is full of abbreviations, embedded lists and semicolon separated limbs, so it would put a guessed boundary inside the one tier of the model that is meant to be deterministic. Nobody cites the second sentence of a clause either, so it buys no query. Where sentence precision is wanted, for highlighting rather than addressing, it comes from character offsets into a leaf's text, which is the same mechanism references and term uses already use. The footers also carry per part template versions, Core Terms at v3.0.11 and each schedule with its own Model Version, which is evidence that the pack is an assembly of separately versioned templates. The graph stores that per part version, because it is what an amendment will arrive against.

Every node and edge carries the `batch_id` of the ingestion run that created it. A batch can be rolled back in one operation, which is the cheap insurance that makes continuous ingestion survivable. This pattern, and the composite uniqueness constraints behind it, I carried over from my own product's loader, where the same mechanism scopes tenants rather than versions.

## 4. The pipeline

Eight stages. Each is a separate CLI with a JSON contract on both sides, so stages can be rerun, tested and swapped independently. The expensive and non deterministic stages are pushed as late as possible.

**Stage 0, profile.** Free, instant. For each document and page, does a text layer exist, is the PDF tagged, does an outline exist, what does the font size distribution look like. The profile routes the document. Native text goes to the deterministic parser. Pages without a text layer would go to a layout OCR model such as Docling, which this document never needs, and the profile is stored on the `DocumentVersion` node so downstream consumers know what kind of extraction their answers stand on.

**Stage 1, parse.** Deterministic, PyMuPDF. Words with boxes per page, headers and footers stripped by position and repetition, part boundaries detected from the per part title and footer signature changes, numbering captured with its box. Tables and forms are detected and kept as tables with cell boxes rather than flattened to prose. Output is one layout tree per part, every node carrying text, page and boxes.

**Stage 2, assemble.** Deterministic. The flat numbered lines become the tree. Top level clause headings have the shape "3. What needs to be delivered", body numbering the shape "3.1.2", lettered paragraphs "(a)". The assembler enforces the invariants, children nest under their numeric parents, siblings ascend, no gaps without a recorded exception, and every violation is logged rather than repaired silently, because the violations are themselves a quality signal. The Award Form and similar parts assemble as forms, numbered rows of label and value cells, not as clause trees.

**Stage 3, references.** Deterministic first, LLM residue second. Regexes with about a dozen shapes find candidate references, the scope rules from 2.1 above resolve most of them, and what survives, bare schedule numbers without titles, ranges, "of that Schedule" chains, goes to an LLM with the top candidates presented together and an explicit permission to answer none. The two layer contract, deterministic pass stamping its method, model only on survivors, candidates shown together, none as a valid answer, confidence recorded, is lifted directly from the incremental entity matcher I built for my own graph product, where the deterministic layer covered about 60 percent at effectively perfect precision.

**Stage 4, vocabulary.** As described in tier 2. Definition site discovery, term matching, ambiguity routing, stratified audit sampling.

**Stage 5, concepts.** As described in tier 3. Per section LLM scan, embedding resolution, ABOUT edges. The only stage that is generative over open text, and the only one whose output is flagged non citable.

**Stage 6, embeddings and summaries.** Retrieval needs vectors at more than one altitude, because a query like "who owns the intellectual property" should land on clause 9 while a query quoting a phrase should land on the leaf that contains it. Leaves get their raw text embedded, which is precise and deterministic. Containers get two vectors, one over the concatenated text of their subtree where that fits a sensible token budget, and one over a short generated summary. The concatenation keeps a container findable by its own words, and the summary is what gives altitude, since an embedding averaged over a very long clause drifts toward nothing in particular. Embedding summaries as well as text is a retrieval decision I have made before, and the two serve different moments in a query.

Two rules keep this from leaking into the trust gradient. A summary is generated text, so it sits in the same trust class as a concept, and a hit on a summary vector has to resolve down to leaf text before anything is cited from it. And the vectors live in a search index keyed by node id rather than on the graph nodes themselves, which lets the corpus be re embedded on a new model without touching the graph, and lets retrieval scale separately from storage. That is the same split I moved to in my own product, for the same reasons.

**Stage 7, load.** Neo4j through MERGE only, constraints first, batch tagged, rollback able. A JSON export of the same graph ships alongside, so nothing about the design depends on the engine. The same loader contract would target ArangoDB or Neptune, and in a sovereign deployment the whole pipeline runs inside the boundary, the only outbound dependency being the model endpoint, which is exactly the component Whitespace's platform exists to bring inside.

**Stage 8, evaluate.** Runs after every load, detailed in `EVALUATION.md`. Structural invariants, three oracle diffs (the given page map, the embedded outline, the given definitions list, each of which the pipeline must derive independently), a hand labelled golden set for references and terms, and the stratified audits.

Cost and failure profile in one line each. Stages 0 to 2 are free, deterministic, and fail loudly on malformed layout. Stage 3 is deterministic for most references, pennies of LLM for the residue, and its failure mode is a wrong resolution, which is why confidence and candidates are stored. Stages 5 and 6 are the LLM spend and are throttleable, skippable and per part parallel. Stage 7 is I O bound. Nothing in the hot path is a fine tuned model, so there is nothing to retrain when the corpus changes.

## 5. When the document fights back

- No table of contents, no trustworthy outline. Nothing depends on either. This document ships a 498 entry outline, and the pipeline treats it purely as an oracle to diff against, because sampling showed it degrades below the top level in exactly the schedules whose source styling was sloppiest. The diff triages disagreements into parser wrong, outline wrong, or both, per part.
- Scanned or degraded pages. The stage 0 profile routes them to a layout OCR path, and the provenance records that the text stands on OCR so confidence can be discounted downstream.
- Inconsistent numbering. Assembly invariants log every violation. The Core Terms contain a real typo, "3. rFramework" on page 24's form and a heading whose number detached from its period, and the right behaviour is recording an exception, not silently repairing it.
- Forms and tables. The Award Form is label and value rows with placeholder text, not clauses. The genre is detected in stage 1 and modelled as a form, and placeholders like "[Insert name]" are preserved, since in an executed contract those cells are where the parties, dates and money live.
- Two column layouts. The definitions schedule wraps terms mid cell across lines. Column detection runs on box geometry, not on line order.

## 6. Scale. The second, hundredth and ten thousandth document

**The second document** is the interesting one, and the build demonstrates it live. I ingest the slice as four successive batches, Core Terms first, then the definitions schedule, then the Award Form, then a call off schedule. After batch one, references out to schedules that have not arrived yet sit unresolved. When their targets land in later batches, the resolver re runs over dangling references and they flip to resolved, with the transition counted in the eval report. That is the amendment and arrival story in miniature, and it falls out of references being nodes with status rather than edges that either exist or do not.

**The hundredth** is about idempotency and money. Deterministic ids make re ingestion an upsert. Batch tags make any load reversible. The LLM spend is confined to the reference residue and the concept tier, both throttleable, and everything else is CPU. Per part parallelism is trivial because parts are independent until reference resolution, which is a join at the end.

**The ten thousandth** is about variety and drift. New drafting houses break the definition convention and the numbering grammar, which is why both are versioned rule sets evaluated against oracles per document, and why the profile stage exists to route rather than assume. Amendments arrive as new `DocumentVersion` nodes with a `SUPERSEDES` edge. The tree diffs against the previous version by lineage key and content hash, so an untouched provision is recognised as the same provision across versions even though each version holds its own instance of it, and a point in time query is a filter on version rather than a separate store. The cost is real. Storage grows with versions times provisions, and if that ever bites, the fix is to mint a new instance only when the content hash changes and let unchanged provisions span versions on a validity edge. I would measure before doing that, because the simple model is far easier to defend in an audit. What breaks first, honestly, is the reference resolver's precision on cross document references, when "Schedule 2" could belong to any of ten thousand frameworks, and the answer is that resolution becomes scoped search within the citing document's family plus a review queue, not a bigger model.

## 7. The human in the loop

Two places, chosen because they compound. First, the review queue. Every ambiguous or unresolved reference and every ambiguous term match lands in a small UI showing the source sentence, the page image with the box drawn, and the candidates. The reviewer picks or rejects. Second, the same decisions become the golden set. Every human decision is a labelled example, so the eval set grows as a by product of operating the system rather than as a separate annotation project, and the fine tuned term matcher trains on exactly those labels later. A reviewer's hour spent unblocking today is also an hour of ground truth for regression testing forever.

## 8. The build slice, and why

Core Terms, pages 1 to 22, the floor the brief names, plus the definitions schedule, the Award Form and Call-Off Schedule 9, about 80 pages, run as the four incremental batches described above. The definitions schedule is what makes the vocabulary tier real, the Award Form is a different genre with tables and placeholders, and Call-Off Schedule 9 brings references that cross a document boundary, which is where the notes say resolution stops being trivial, and I agree. The deterministic stages alone also run across all 475 pages at no LLM cost, producing the derived page map, the outline diff and per part invariant violations, which is the concrete evidence for what would and would not survive the rest of the document.

One check that run settles. The notes say 46 constituent parts, the provided page map lists 48 rows, and my derived map reports what the document itself supports.

## 9. Models per stage

My default is the smallest model that survives the eval, chosen per stage, with the judge a different model from the extractor.

| Stage | Model | Why |
|---|---|---|
| 0 to 2, 4 parse, assemble, vocabulary | none | Deterministic. |
| 3 reference residue | Claude Haiku 4.5 | Bounded choice among presented candidates. Escalate single hard cases to Sonnet 5. |
| 5 concepts | Claude Sonnet 5 | Open generation, needs judgement, not frontier cost. |
| 6 summaries | Claude Haiku 4.5 | Compression, not reasoning. |
| Embeddings | local sentence transformer | Sovereign by default, no data leaves the boundary. |
| Eval judge | Claude Haiku 4.5, separate prompt | Independence from the extractor matters more than judge size, human golden set anchors it. |
| Chat agent | Claude Opus 5 | The demo surface, tool use quality dominates. |

<!-- Dan, this table is yours to reset. The reasoning column is the part they will ask about. -->

## 10. Thought through, guessed, would prototype first

**Thought through.** The three tier model and its trust gradient. Reference nodes with status. Identity and idempotency keys. The scope rules, which come from the document itself. Batch tagging and rollback. The eval oracle diffs. The staged ingestion demonstration.

**Guessed.** That the definition discovery convention transfers across CCS frameworks. That term matching ambiguity concentrates in single word terms in heading position. That concept resolution needs only embedding clustering at this corpus size. Each guess is written next to the evaluation that would confirm or kill it.

**Would prototype before committing.** The structural diff between document versions, which looks clean on paper and is notoriously fiddly on real amendments. Cross document reference resolution at corpus scale. The fine tuned term matcher, where the open question is training data volume, and which model family to start from, which is scoped as a research task in the agent workstream.

## 11. Time spent

Filled in at submission. The design and this document took the first block of the evening, the build ran as an orchestrated agent fleet against the committed spec, and the honest total is recorded here rather than rounded down.
