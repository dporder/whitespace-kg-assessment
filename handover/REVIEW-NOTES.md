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

