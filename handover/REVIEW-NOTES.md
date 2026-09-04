# Review notes

What came back from agents, what I accepted, what I rejected, and what I changed. Kept as I went
rather than written up afterwards. The brief asks whether I am a critical reader of agent output,
and this is the honest record of that rather than a claim about it.

## 1. Research memo on legal document hierarchy

**Agent.** Background researcher, told to work from primary sources and to flag anything it could
not verify rather than guessing.

**What it did well.** It went to the normative Akoma Ntoso XSD rather than to summaries of it, and
came back with the exact list of 27 hierarchy elements, the fact that the standard is a vocabulary
with no fixed ordering, and the branch or leaf content model. That last one independently confirms
the leaf only text decision I had already made on other grounds, which is the most useful thing in
the memo. It also found a Court of Appeal judgment, Al Mana Lifestyle Trading v United Fidelity
Insurance [2023] EWCA Civ 61, where the parties had to add their own bracketed numbers to a clause
because sentences carry no native identifier. That is much better evidence for not modelling
sentences than the reasoning I had.

**What I rejected.** It stated that RM6116 reaches four decimal levels, citing `9.1.3.2`. I checked
and there are zero four level numbered lines in the pack. The claim is wrong and the cited example
does not exist. Corrected in the memo with the check that disproves it.

**What I corrected in my own work because of it.** My spec said there was exactly one roman numeral
item, which was true of the Core Terms and false of the pack, where there are 82. That was my error,
not the agent's, and the memo caught it.

**What checking it turned up.** Following up its claim that the pack calls `1.3.8` a Clause led to
a real inconsistency in the document worth designing around. See the memo's verification section
and the named resolver case in `SPEC.md`.

## 2. Contract reconciliation before fan-out (orchestrator, build session)

Spec changes land spec-first per `CLAUDE.md`, so these went into `SPEC.md` and `schemas.py`
together, before any worker spawned.

**Fixed, a real contract bug.** `schemas.py` forbade all children on `intro` nodes, but the spec
says ref children annotate any text-bearing node, and a lead-in like "Subject to Clause 26, the
Supplier must:" plainly contains a citation. The validator now distinguishes anatomy children
(forbidden on intro and cell) from ref children (allowed wherever there is text to anchor them).

**Pinned, where workers would otherwise have guessed differently.** The id formulas
(`sha1("{doc}|{version}|{path}")`, helpers in `schemas.py` so there is one implementation), the
hash-only text normalisation, the `intro` path segment, `order` as per-part preorder, the stage 3
output shape (`RefsFile`, flat, because stage 2 trees carry no refs and stage 7 attaches by path),
the graph edge JSONL row (`GraphEdge`), `definition_used` as the governing site's scope string,
and `ASSOCIATED_TERM` computed in stage 7, since it joins stage 4 and 5 outputs which must not
read each other. Added the missing `Legislation` model from SPEC 2.2. Allowed `title` on parts
(display name; previously headings only). Tightened validation to the full kind table: kind-scoped
fields rejected on other kinds, cells require text and grid position, tables require dimensions,
form rows and tables hold only cells, documents hold only parts.

**Fixtures decision.** Fixture text is synthetic mimicry rather than PDF excerpts, because the
SPEC ground rule forbids copying document content into the repo outside `output/`. The structures
are the document's real ones (bare grouping heading, intro sandwich, grouped list refs, unresolved
and ambiguous and external refs, delegating definition, alias, form typo). Recorded in
`fixtures/README.md` with the consequence for UI crops (real ink at those coordinates, not this
text, until real output lands).

**Kept as-is, consciously.** The "discriminated union" in the spec is implemented as one model
plus one validator enforcing the per-kind table, not a pydantic union of twelve classes. One
schema, one walker, one id scheme is the stated point of the design; the discrimination lives in
the rules.

## 3. Research memo on NER model selection (build session)

**Agent.** Researcher, primary sources only, no code, told to end with a shortlist and a deciding
experiment built on the labels this repo actually accumulates.

**What it did well.** It reframed the problem before comparing models: with a closed authoritative
vocabulary this is disambiguation plus recovery, not open-vocabulary NER, and it grounded that in
the shipped GLiNER checkpoint config rather than the paper. Two findings materially change the
plan. First, `golden/decisions.jsonl` structurally cannot measure recall, every row descends from
matcher output, so the deciding experiment now starts with roughly ten blind-annotated pages to
mint the missing denominator. Second, GLiNER's flat decoder is greedy-by-confidence, not
longest-match, so the spec's determinism guarantee would have to be re-imposed in post-processing
whatever model wins. It also caught its own near-miss: a search snippet implied gazetteer features
give +28.8 F1, the fetched paper says +0.52, and the memo carries the verified number.

**What I verified before accepting.** The two load-bearing claims, checked against the primary
sources directly, not the agent's word: the `gliner_medium-v2.1` config does carry
`max_types: 25`, `max_len: 384`, `max_width: 12` and backbone `microsoft/deberta-v3-base` (which
is what makes the DeBERTa control arm clean), and the Legal-BERT card does state cc-by-sa-4.0,
uncased, US contracts from SEC EDGAR with UK material being legislation. Both hold exactly. Repo
hygiene also checked: only the memo file created, no em dashes, unverified claims flagged in
their own section rather than smoothed over.

**Accepted.** Recommendation adopted for the design surface: no replacement plan, a zero-shot
recall net beside the deterministic matcher, single entity type with string linking, longest-match
kept as arbitration, licence confirmed per checkpoint. Its note that the 40-sample audit cannot
certify a model switch (Wilson interval wider than the gate) is worth carrying into any future
tuning of `AUDIT.confident_term_sample_size`.

**Late-arriving sweep, folded in as an addendum.** The researcher had a licence-and-evidence
sweep of the legal encoder family still running when it reported; it landed afterwards with two
exclusions that matter commercially (`casehold/*` ships with no licence grant at all, verified
against the HF API myself before accepting; `pile-of-law/*` is NC-encumbered), confirmation that
no English-contract encoder exists anywhere, and a review showing the domain-pretraining gain for
contract token classification is 1 to 2 F1, mostly self-reported by one group, dominated by
architecture and annotation quality. Shortlist unchanged; the memo's Legal-BERT exclusion now
stands on stronger ground. Integrated by the orchestrator, marked as an addendum, resolved items
annotated in the memo's unverified list.

