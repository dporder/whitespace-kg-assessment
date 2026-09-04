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

**Correction, build session: the rejection above was itself wrong, and the researcher was right.**
The parser-builder's corrected grammar surfaced 46 four level dotted lines, concentrated in
Call-Off Schedule 6, Call-Off Schedule 22 and Joint Schedule 8, and the orchestrator re-verified
independently by direct extraction: `9.1.3.2 any existing law, statute, rule or regulation...`
sits on page 202, exactly as the memo cited. The original disproving check must have run against
a text extraction or pattern that missed these lines. Config (`max_dotted_depth` 3 to 4, a fourth
dotted numbering pattern), SPEC section 4, DESIGN.md and the memo's verification section are all
corrected. Left in place above rather than rewritten, because the point of this file is the
honest record: the critical-reading process caught a real fabrication risk, then a later, better
measurement caught the check itself. Both halves are the lesson.

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

## 4. Stage 8 eval harness (eval-builder, three rounds, merged)

**Round 1.** Harness landed complete: ten SPEC 2.6 sections over fixtures, gates with exit codes,
no clock anywhere (an inputs fingerprint instead). Its first run caught two defects in my own
fixtures, an unexplained excerpt numbering gap and a term used but never defined. Both fixed on
master. It also settled the 46-versus-48 question two ways: the notes' own table and the PDF's
embedded outline both say 48, the prose's 46 is the odd one out.

**Tester gate, failed correctly.** Two branch tests encoded the pre-fix fixture state; the tester
proved causation by bisecting fixtures rather than inferring. Sent back; the builder re-baselined,
made the dangling-use check seed its own condition rather than depend on a broken fixture, and
replaced a determinism test that described byte-identity without checking it with a real cold-run
byte comparison.

**Adversarial review, three blockers, all proven with executed probes.** A recorded anomaly
granted blanket amnesty (prefix match on node, sibling, or parent) so a parser silently dropping a
clause passed the zero-tolerance structural gate; the abstention gate published a bare int and
passed green when zero unresolvables were labelled; an omitted chosen_candidate scored refs as
parser failures and graded terms against the pipeline's own answer. Plus a duplicate rate that
could exceed 1.0 (pairs summed into a member count), outline agreement double-counting, a
fingerprint that omitted the artifacts two sections diff against, and an unbounded
simple_cycles enumeration that would hang on the real JS1 vocabulary.

**My rulings.** Anomaly explanation semantics: only on the two nodes a check compared, one anomaly
explains one violation, and an anomaly naming a different follower than observed does not match.
Abstention as a Rate over golden unresolvables so no-data flows to skipped, never pass.
chosen_candidate required exactly where the format says, malformed otherwise, never defaulted to
pipeline output. Union-find for duplicate clusters. Cycle enumeration capped per SCC with honest
disclosure. Accepted two builder judgement calls: anomaly_index required for node-anomaly verdicts
only (triage rows key on their queue id), and default scope inferred from the output's own batch
ids when unambiguous.

**Merged after my own gate run**, not the builder's word: 127 tests green and the CLI at exit 0 on
the integrated state in a scratch worktree, and the amnesty regression test read line by line to
confirm it encodes the reviewer's probe rather than a tautology.

## 5. The two UIs (ui-builder, two rounds, merged)

**Round 1.** Design-first held: two canvases, three directions each, one chosen with recorded
reasoning, then both apps built against fixtures with real page crops, a working decisions write
path, and 134 tests. The tester verified the mechanical stack end to end and called it safe.

**The reviewer proved otherwise, and this is the entry worth reading twice.** The UI's verdict
vocabulary was invented independently of the eval harness's golden format: zero overlap, proven by
running the UI's own decisions file through the real loader, four records in, zero usable. The
`unresolvable` verdict, the one the zero-tolerance abstention gate feeds on, was unreachable from
the two-button UI. Two anomalies on one node silently superseded each other. A citation with an
empty or non-numeric page rendered as verified. All four invisible to both sides' tests, because
each side tested only itself: 134 green tests pinning the wrong contract is why the tester gate
stayed green, and why the adversarial reviewer role exists.

**My ruling, spec-first.** The golden vocabulary is now pinned in SPEC 6 (ref
target/unresolvable/not_a_reference with chosen_candidate required on target; term use/not_a_use
with the governing term in chosen_candidate; anomaly confirmed/rejected keyed by anomaly_index,
triage rows keyed by queue id). The eval loader's GOLDEN_FORMAT.md elaborates it; the UI adopts
it verbatim.

**Round 2, accepted.** The builder owned the miss plainly, adopted the vocabulary, made every
verdict reachable with per-kind controls (four for refs, a governing-term picker for alias
collisions), required anomaly_index, closed the verifier hole (page_unparseable is a failure,
never ok), made zero-citation answers warn instead of claiming verification, and added anti-drift
tests that assert its verdict tables EQUAL the harness's at test time, so the seam cannot reopen
silently. Cross-validation now runs the UI's output through the real loader: seven verdicts, six
loaded plus one legitimately superseded, zero unrecognised. A runtime CHAT_SCRIPTED mode was
added after the tester found scripting existed only inside pytest; I verified a full scripted
exchange over real HTTP myself before merging, four citations, all ok, crops resolving. Merged
after my own gate run on the integrated state: 285 tests green, decisions defaulting to
golden/decisions.jsonl per spec.

## 5b. UI redesign rounds (ui-builder, Dan's design review driving them, merged)

Dan reviewed the first canvases as the operator and reported genuine confusion, which became the
brief: the bar is a non-technical domain expert, UX writing is the deliverable as much as pixels.
Round: rows compose their own plain-English question server-side (the Schedule 2 row now asks
"Which one does the writer mean?" with candidates described in human terms), paths and bare
scores banished to disclosures, per-claim footnotes and one evidence panel in chat, a
connections graph view in the brand's process vocabulary, both registers per the brand memo.
The gate on that round split usefully: the tester passed everything mechanical, the adversarial
reviewer proved two blockers under the green (the connections endpoint's tool-only claim was
false for node labels, which read the corpus directly; a term-row field the server never sent),
plus an untested copy layer citing a test file that did not exist. All closed: naming now flows
through the tool layer (get_provision, follow_references, define and cite all report the name
the agreement uses, both backends sharing one composer), the copy layer has 26 tests including
one that deliberately typos a verdict, and Dan's second-round refinements landed (counted tabs
so the page shows one review type at a time, prose behind disclosure, the unresolved-ref row
reframed from error-reading to "confirm now and the link connects when that schedule arrives",
with a test asserting no error-vocabulary in that row). Chat locked as approved by Dan.
Merged after my own verification run: 316 tests on the integrated state, governing_citation
live-checked, answer_graph labels tool-derived with external edges distinct. One recorded edge
case: a path passed directly to answer_graph that only exists as an external referent renders
unsettled (first-add wins); in the real flow legislation enters via its edge and reads external.

## 6. Parser stages 0 to 2 (parser-builder, round 1 back, config corrected, fix round running)

**What it delivered.** Stages 0 to 2 complete: Core Terms parses to 444 nodes with zero
unexplained geometric violations, and the full 475 page structural run derives 48 parts covering
every page with no gaps, the third independent witness for 48 against the notes' prose claim of
46 (after the notes' own table and the embedded outline). 27 parts assembled, 21 refused by the
fit checks, every refusal named with evidence.

**Two real bugs it found in my config, fixed in config with the evidence recorded.** The item
pattern required leading whitespace a PDF text layer never emits (indentation is geometry, not
characters) and matched zero of Core Terms' 169 lettered items; it also demanded an opening
bracket most schedules do not print ("a)" not "(a)"). Dotted numbers needed an optional trailing
period for Framework Schedule 1's "1.1." style. The builder measured each variant across the pack
before proposing, did not touch config itself, and reported instead, exactly per contract.

**One spec claim of ours did not survive the parser.** The "34 of 35 headings" quirk does not
reproduce in Core Terms, whose headings are typographically identical; the real detached number
case is Framework Schedule 5's "2   Reporting period". Also 146 two level provisions in Core
Terms is actually 144, the other two being cross references at the start of wrapped lines. SPEC
corrected both ways; the quirk list is now something the parser proved rather than something it
inherited.

**Ruling: per part fit checks, spec-first.** The pack is a binding of ~48 separately versioned
templates without one shared numbering house style, so the five fit checks now also run per part,
parts failing quarantine individually with no override flag, the document verdict still computed.
B4 (Call-Off Schedule 9) was refused at 19.1% unmatched under the broken item pattern; the config
fix is expected to clear it, being re-measured now.

**Findings worth keeping.** The definitions schedule is missing ink in the source itself: 224
term cells carry a closing quote with no opening one, and in wrapped cases the first letter is
genuinely absent from the page (verified by rendering at 4x). Recorded verbatim, never repaired.
And two of the geometric violations the parser refused to explain away are the document's own
typesetting, Call-Off Schedule 9 really prints 5.2.1 left of its parent 5.2, true ink, correctly
reported.

## 7. Whitespace brand-language memo (researcher with subagents, accepted)

Commissioned after Dan's design review of the two UIs, with the research fanned out to
per-surface subagents per his instruction. The memo reads the palette, gradients, radius and
typography literally from white.space's shipped CSS custom properties rather than eyedropping,
pixel-samples the product screenshots for the surfaces the stylesheet does not cover, and leads
with the finding that matters: Whitespace has two visual registers, marketing (navy, lavender,
gradients, pills, the trailing underscore) and product (neutral near-black, one flat action
blue, 10px cards, no underscore anywhere), and the failure mode is styling a product surface in
the marketing register. The researcher verified its own subagent's claims and caught two errors
(a wrongly-reported-unused font, a wrong scale divisor), settled the screenshot scale
empirically, and lists every unverifiable thing (the real app's stylesheet is behind auth, so
hover/focus/disabled states are unknown). Accepted; the ui-builder styles both surfaces from it.

## 8. Closing the stage 2 to stage 8 seam (both builders, merged together)

First contact between the parser's real output and the merged eval harness reported 261
unexplained violations at batch scope where stage 2 reported zero, over identical trees. Neither
side was broken alone: they had invented disjoint names for the same checks, used different
geometry tolerances (the harness's spec-silent 0.5pt against the parser's configured 2.0), and
the parser's manifest sat where the harness enumerates part files. The fix was contract, not
code heroics: the check vocabulary, the complete tolerance map (read out of the parser's actual
implementation, including a vertical tolerance that had lived as a module constant), and the
manifest location are now pinned in SPEC 2.1 and config, with both stages reading one source.

Two judgement calls worth the record. The parser named and avoided a trap while renaming: an
unexplained violation stamped with a bare check-id key would prefix-match the harness's ledger
and turn every complaint into its own excuse, so unexplained ones are keyed unresolved_<check_id>.
And the final residual, one violation, was settled by the orchestrator enumerating the harness's
own check over the real trees rather than another agent round: the JS1 definitions table has no
ink of its own, the harness compared its cell-derived extent against the parent's own indent, and
the ruling (own-box checks compare own ink only, containment is extent_nests's job) matched what
the parser already did. The harness kept its extent fallback for sibling checks deliberately,
extent against extent being like with like, with a test pinning that coverage.

The combined check on the merged state: 435 tests, profile, parse and assemble at exit 0 over all
four batch parts, and the structural gate passing at observed=0. The parser's reviewer cycle also
belongs to this story: two blockers (intros inserted in reverse reading order, caught by the
branch's own invariant checker; a per-part fit that ran two of five checks on cap-truncated
evidence) fixed with regression tests, the verified-typesetting ledger re-keyed to node identity,
and the honest outcome that the real five-check fit quarantines 6 of 48 parts, five of them for
geometry the ledger independently verified as the document's own typesetting.

## 9. Stages 3 and 7, references and graph load (resolver-builder, gated, fix round running)

**What came back.** Both stages complete and run on the real document, not just fixtures: 139
references detected in Core Terms (111 clause, 14 schedule, 12 legislation), 108 resolved by the
stipulated scope rules, 24 unresolved, and every one of the 24 is a citation out to a schedule
that has not been ingested yet, which is the batch-arrival demonstration sitting ready to flip.
Legislation parsing handled all three shapes on real ink, including the parenthesised-qualifier
trap. The load put 591 nodes and 1054 edges into Neo4j reconciling exactly against stage outputs,
with rollback and sweep proven on the live graph. Its real run surfaced three bugs its fixtures
never could, the best being rollback deleting shared referents out from under other batches'
edges. Four spec gaps it stopped and reported (range-member ref paths, heading-title citations,
ref box granularity, where the document root materialises) were ruled and pinned spec-first.

**The gate split, again, and both halves earned their keep.** The tester passed it end to end and
proved the milestone: the chat UI answered "What does Clause 9.2 say?" against the real graph,
both citations verified, the quoted text byte-identical to the source PDF. It also proved the
suite makes zero live API calls by running it under a socket-level egress block, and filed two
defects that bite exactly when batch 2 loads (the synthesised document root's id varies by which
part infers the version; a same-batch reload cannot converge). The adversarial reviewer then
found two blockers underneath the green: sweep's orphan-referent cleanup is a global DELETE
ignoring batch and scope, the same bug class as the rollback fix, still live in a second path;
and the loader reports edges it never created (an endpoint MATCH that misses writes nothing and
is still counted) while reconciliation forms no edge expectation at all, so the spec's edge-count
gate could not fail. Salience also proved per-batch where the design says per-graph. All of it is
one fix round, running now; nothing merges until the reviewer's probes pass as regression tests.

## 10. Stages 4, 5 and 6, vocabulary, concepts, embeddings (enrichment-builder, fix round done, gate pending)

**Numbers that go straight into the submission.** 259 declared terms derived from Joint Schedule
1, settling the notes' "roughly 300" as high by 14 percent. 2,756 term uses after the plural
ruling (2,751 confident; 159 resolved by the routing model; 5 held for review). 174 concepts
minted from 190 proposed with the tier-2-outranks-tier-3 guard genuinely firing, nine proposals
blocked for colliding with declared terms. 1,361 embeddings whose cache makes a rerun cost zero
API calls. Concepts were deliberately sampled to the two clause-genre parts on the operator's
cost ruling, with the full-scope path shipped behind a flag and the sampling declared in the
output so the eval reports coverage honestly.

**A ruling the document itself forced.** JS1 1.3.1 stipulates the singular includes the plural,
the same stipulated authority the reference resolver leans on, so inflected matching was adopted
spec-first after being measured (210 candidate uses). The adoption immediately earned its
routing: Party and Parties are both declared terms, the checker overrode the naive
prefer-the-printed-surface default in 52 of 85 collisions, and it caught the one false positive
the plural rule introduced, "Third Parties Act 1999", a statute not a term use.

**Cross-worker finds.** Its real stage 4 run exposed two parser defects in Call-Off Schedule 9's
definition blocks (flattened into prose; columns interleaved on Part B). The parser's root cause
was one line, a rule-thickness cap rejecting the table's heavy border, verified by rendering the
pages before concluding, and the fix also made Call-Off Schedule 6's 24-row definitions table
parse for the first time. The four part-local terms now sit as label cells, verbatim.

## 11. Resolver fix round and merge; the chat demo hardening (both merged)

**Resolver.** All gate findings closed with the reviewer's probes as regression tests: the orphan
referent cleanup now touches only what the load itself asserted (the 4-reported-1-written probe
and the cross-batch orphan survival are both pinned tests), reconciliation forms a real edge
expectation the exit code gates on, with deferred edges (far endpoint's batch not yet arrived)
partitioned and reported rather than silently uncounted. The document root is pinned to
"as-bound" rather than promoting one part's footer version, on the builder's own reasoning that
the pack binds ~48 separately versioned templates and the root asserting v3.0.11 would claim
something the document does not say, accepted as better than the ruling it replaced. Same-batch
reloads converge via a load-content hash. Its four-part run then proved the demonstration
mechanics live: 1,843 nodes and 3,299 relationships converging on rerun, 39 cross-part resolved
references, and salience visibly recomputing when a citing schedule arrives, core-terms/27
rising from 1.946 to 4.159. A first four-part stage 3 run also surfaced nine same-span duplicate
ref paths (LLM spans overlapping grammar citations), now deduped at emission under a generalised
span ordinal pinned in SPEC 2.2.

**Chat, driven by the operator's live testing before his presentation.** Dan's dead-citation
report traced to two stacked causes the tester had flagged as a hazard: the run-directory picker
choosing a cache directory, and all-or-nothing source loading that let a missing refs/ take
working crops down with it. Fixed per-artifact with the loaded state reported in the health
endpoint. His trace-copy complaints became a shared gap-wording table both backends must use,
with the Neo4j side distinguishing, in-graph, a term with no definition site loaded from an
unknown term (by DEFINED_IN edges, not labels, since uniqueness constraints pre-create empty
labels). Browser-level audit also caught Enter-to-send missing, the connections view collapsing
into overlapping pills past a dozen nodes (replaced with a deterministic no-overlap layout), and
fixture vocabulary leaking into suggestion chips against real data. Every fix verified in a
browser against the live graph before merge.

## 12. Enrichment gate and final round; the fleet's last merge

The enrichment gate found one root cause behind all 26 integrated-suite failures, and it is the
best single catch of the build: the branch's tests faked pipeline/llm.py via sys.modules, but
once any test genuinely imports it Python binds the submodule as a package attribute and `from
pipeline import llm` never consults sys.modules again, so the fakes were silently bypassed in
both directions and `pytest tests` was making live billable API calls. The builder's fix went
beyond the gate's verified patch: a shared seam helper patching both places, plus an autouse
tripwire fixture that fails any enrichment test reaching the real client with instructions,
which immediately caught twelve more silently-leaky tests. Proof is behavioural, the full suite
byte-identical under valid and sabotaged keys.

The gate's other two finds both fixed in the same round. The shared client's 1024-token default
truncated the routing batches mid-JSON, losing 85 percent of verdicts to honest degradation:
routing now sizes its budget per batch and detects truncation structurally, 169 of 169 verdicts
obtained on the re-run. And Call-Off Schedule 9 defining "Breach of Security" once per sub-part
broke the (term, scope) uniqueness SPEC 2.3 assumed: the ruling made scope the nearest scoping
ancestor's path, and the builder improved it in implementation, scoping to the nearest division
the drafters themselves name as a Part rather than the literal nearest heading, because the
literal reading left paragraphs 2 to 5 outside the definition that plainly governs them. The 24
affected uses now split 10 to Part A's scope and 14 to Part B's, and a term genuinely defined
twice at one scope is recorded as a drafting-defect anomaly on both sites, never key-overwritten.

Merged after my own verification: 857 tests passing identically under valid and sabotaged keys
on the integrated state. Final four-part numbers: 265 definition sites, 2,803 term uses at zero
violations, 122 concepts under the operator's sampling ruling, 1,369 embeddings, everything
byte-deterministic. With this merge every builder branch is integrated.

**Late-arriving sweep, folded in as an addendum.** The researcher had a licence-and-evidence
sweep of the legal encoder family still running when it reported; it landed afterwards with two
exclusions that matter commercially (`casehold/*` ships with no licence grant at all, verified
against the HF API myself before accepting; `pile-of-law/*` is NC-encumbered), confirmation that
no English-contract encoder exists anywhere, and a review showing the domain-pretraining gain for
contract token classification is 1 to 2 F1, mostly self-reported by one group, dominated by
architecture and annotation quality. Shortlist unchanged; the memo's Legal-BERT exclusion now
stands on stronger ground. Integrated by the orchestrator, marked as an addendum, resolved items
annotated in the memo's unverified list.

