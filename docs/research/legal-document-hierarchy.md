# Legal document hierarchy: structural node types for RM6116

## 1. Akoma Ntoso / OASIS LegalDocML

**27 named elements, and it is a vocabulary rather than an ordering.** Every element declared `type="hierarchy"` in the normative schema:

`book, tome, part, subpart, title, subtitle, chapter, subchapter, section, subsection, article, paragraph, subparagraph, clause, subclause, division, subdivision, list, sublist, point, indent, alinea, rule, subrule, proviso, level, transitional`

Plus `hcontainer`, a generic container with a required `name`. The schema imposes **no fixed sequence**. Any hierarchy element nests inside any other, and the names pool several traditions (`alinea`, `point`, `indent` are civil-law), so "the AKN ladder" does not exist.

**Intro and wrap-up.** The `hierarchy` type is `num?`, `heading?`, `subheading?` then an exclusive choice: either `intro?`, children (or `crossHeading`), `wrapUp?`, or a single `content`. A container is therefore a branch or a leaf, never both. `intro` introduces sub-items, `wrapUp` follows them; all three are type `blocksreq`.

**Text lives in `p`** (a block, as in HTML) inside `content`/`intro`/`wrapUp`. **No sentence element**, with zero occurrences of "sentence" across AKN 3.0's 315 element declarations. Below `p` sit only inlines (`span`, `ref`, `def`, `term`): they take an `eId`, but carry no sentence semantics.

[Part 1 Vocabulary](https://docs.oasis-open.org/legaldocml/akn-core/v1.0/os/part1-vocabulary/akn-core-v1.0-os-part1-vocabulary.html) · [XSD](https://docs.oasis-open.org/legaldocml/akn-core/v1.0/os/part2-specs/schemas/akomantoso30.xsd)

## 2. UK contract drafting convention

**Most common: Clause > sub-clause in the body, Paragraph > sub-paragraph inside Schedules.** Lettered `(a)(b)(c)` items are conventionally *paragraphs* and roman `(i)(ii)(iii)` items *sub-paragraphs* of the clause they sit in, borrowed from legislative drafting.

**It genuinely varies by drafting house.** Two live conventions: strict (`1` clause, `1.1` sub-clause, `(a)` paragraph, `(i)` sub-paragraph) and loose, where every decimal level is a *Clause*. Loose is more common; RM6116 uses it, calling `1.3.8` a Clause and reaching four decimal levels (`9.1.3.2`). [Adams](https://www.adamsdrafting.com/what-to-call-the-components-of-the-body-of-the-contract/) reports English practice referring to clauses "at several levels, eg 8, 8.1" against the US Article/Section style (403 on fetch, verified via search index only, so indicative); the continental [Weagree](https://weagree.com/clm/contracts/contract-structure-and-presentation/articles-sections-clause-numbering/) disagrees, preferring article/section/subsection.

**Schedules contain paragraphs, not clauses**, near-universal, and RM6116 says so expressly (§4). [OPC *Drafting Guidance* (2024)](https://assets.publishing.service.gov.uk/media/660407d091a320001a82b06b/2024.03.19.Drafting-guidance.pdf) §6.3.10 pairs both ladders: "To add a subsection or sub-paragraph to a section or paragraph". Its §3.6.9 "sandwiches" (opening words, paragraphs, closing words) match AKN `intro`/`wrapUp`.

## 3. Citable units

**The smallest routinely cited unit is the deepest *numbered* provision**, for example "clause 9.1.3.2", "paragraph 4.2 of Joint Schedule 11". Numbering is what makes a unit addressable.

**Sentences are cited, but by description, never by number.** Verified in *Al Mana Lifestyle Trading LLC v United Fidelity Insurance Co PSC* [[2023] EWCA Civ 61](https://caselaw.nationalarchives.gov.uk/ewca/civ/2023/61), which uses "sentence" 66 times, including "the second sentence of the clause". Tellingly, at [4], its bracketed numbers "are not included in the clause, but were added by the parties for ease of exposition". With no native identifier, litigants invented one. OPC §3.4.1 agrees: "each sentence in a clause should be a separate numbered provision. But there is no absolute rule against having more than one sentence in a numbered provision."

**As a unit in a standard: not in AKN, not in UK practice, but yes in Germany**, where *Satz* is first-class (`§ 986 Abs. 1 Satz 2 BGB`). The LegalDocML.de element name is unconfirmed.

## 4. Interpretation clauses

**Highly standard, and relying on one is sound.** [RM6116](https://assets.crowncommercial.gov.uk/wp-content/uploads/RM6116-All-agreement-terms-and-conditions-3.pdf) (475pp), Joint Schedule 1 (Definitions), clause 1.3.8:

> references to "Clauses" and "Schedules" are […] references to the clauses and schedules of the Core Terms and references in any Schedule to parts, paragraphs, annexes and tables are […] references to the parts, paragraphs, annexes and tables of the Schedule in which these references appear;

Clause 1.3.9 adds that references to "Paragraphs" are to the paragraph of the appropriate Schedules. This is operative text, unlike headings (clause 1.3.11), so it is the best evidence of the parties' own vocabulary. **Caveat:** it fixes the *reference* vocabulary (Clause, Schedule, Part, Paragraph, Annex, Table) but is silent on the pack's ~487 lettered and ~82 roman sub-items.

## 5. Legislation citation

Ladder: **Part > Chapter > cross-heading (not citable) > section > subsection > paragraph > sub-paragraph**, and **Schedule > Part > paragraph > sub-paragraph**. Bills have *clauses*, which become *sections* on Royal Assent.

Citation per [OSCOLA 4th edn](https://www.law.ox.ac.uk/sites/default/files/migrated/oscola_4th_edn_hart_2012.pdf): `Human Rights Act 1998, s 15(1)(b)`. Only the outermost abbreviation appears, nested levels being bare brackets (`s`/`ss`, `sub-s`, `para`, `sch`).

[legislation.gov.uk URIs](https://www.legislation.gov.uk/developer/uris): `/id/{type}/{year}/{number}/{division}/{num}[/{sub}…]`, `{division}` varying by instrument (`section`, `article`, `regulation`, `rule`). Examples: `/id/ukpga/1975/63/section/1/1/ba`, `/id/ukpga/2005/6/schedule/1/paragraph/2`.

## Implications for the RM6116 model

1. Use one `Provision` node with a `level_kind` label (Document, Clause, Schedule, Part, Paragraph, Sub-paragraph) rather than 27 AKN classes; `hcontainer` concedes a fixed ladder does not survive real documents.
2. Draw that vocabulary from Joint Schedule 1 clauses 1.3.8 and 1.3.9, defensible as the parties' own stipulation, extended only for the lettered and roman items it leaves unnamed.
3. Adopt AKN's branch/leaf split: a node holds either children plus optional `intro`/`wrapUp`, or leaf `content`. Same shape as the OPC "sandwich", so doubly attested.
4. Key nodes on the citation path (`Core Terms cl 9.1.3.2`, `Joint Sch 11 para 4.2`), mirroring legislation.gov.uk URIs, since numbering is what practitioners cite.
5. **Do not make sentences nodes.** No standard defines them and no UK citation form numbers them; *Al Mana* shows litigants inventing identifiers ad hoc. Store sentence offsets on the leaf.

---

## Verification against the document (Dan, 3 September 2026)

I ran the memo's factual claims about RM6116 back against the extracted text rather than taking
them on trust. Three held, one did not, and the check turned up something better than the claim it
replaced.

**Wrong. "RM6116 reaches four decimal levels (`9.1.3.2`)."** There are zero four level numbered
lines in the pack, and zero five level. The deepest dotted numbering is three levels, which matches
the example the assignment's own notes give (`10.4.1`). Counting lettered and roman items below
that, the pack is four addressable levels deep, which is probably what both the memo and the notes
meant. The specific number cited does not appear.

**Held. Roman numerals are a real level.** 82 roman items and 522 lettered items across the pack,
against the memo's estimate of ~82 and ~487. My earlier count of one roman item was scoped to the
Core Terms only and was misleading. The grammar needs both.

**Held, and worth more than it looks. The interpretation clause is silent on lettered and roman
items.** So vocabulary can be derived from the document for the named units, and has to be supplied
by the profile for the unnamed ones. That boundary is now explicit in the spec rather than assumed
away.

**Not used here, but kept in the profile. Closing words after a list.** AKN `wrapUp` and the OPC
"sandwich" are real and common in legislation. RM6116 does not use the pattern, with a single
candidate occurrence that turns out to be line wrapping. So `wrapUp` stays a capability of the
hierarchy profile rather than something built for this document.

**New finding, from checking the memo's claim that RM6116 calls `1.3.8` a Clause.** The pack is
inconsistent about this, and the inconsistency is load bearing. References of the form
"Paragraph 1.x" appear 35 times against 3 of "Clause 1.x", and "paragraph N of this Schedule"
appears 27 times, so the stipulated convention in Joint Schedule 1 paragraphs 1.3.8 and 1.3.9 is
what the drafters mostly follow. The three exceptions matter more than the 35 conforming cases,
because a reference reading "Clause 1.2" inside a Schedule resolves under 1.3.8 to Core Terms
clause 1.2, when the drafter may well have meant the local paragraph 1.2. A resolver that follows
the stipulated rule confidently gets those three wrong and says nothing. This is now a named case
in the spec, resolved to the stipulated target, flagged ambiguous, and sent to review with both
candidates attached.
