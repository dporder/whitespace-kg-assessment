# NER model selection for the term matching scale path

Scope: which model family to test first when the deterministic matcher in tier 2 stops being enough. Verified against primary sources; anything I could not confirm is marked. Numbers read off figures rather than tables are marked approximate.

## 0. The framing changes the answer, so it goes first

**This is not open vocabulary NER, and treating it as such picks the wrong model.** The vocabulary is closed and authoritative: Joint Schedule 1 declares roughly 300 terms, the discovery rule adds inline ones, and the definitions schedule is document content that must enter the graph regardless. A model is therefore not being asked to *discover* term strings. It is being asked to do two much narrower things, which have different costs and want different tools:

- **Disambiguation.** A capitalised span has already been proposed by the matcher. Is this use a real use, a sentence initial capital, a heading, or a stray capital from a typo? One fixed label, decided from context.
- **Recovery.** A term use the matcher structurally cannot see: `call-off contract` in lower case, or `Call-Of Contract` with a typo inside it. Nothing for a case sensitive exact matcher to key on.

**Only recovery needs a span extractor.** Disambiguation is span classification over a candidate the matcher already found, which is a smaller learning problem than NER and does not need a NER model at all. Reporting one blended F1 over both would hide which half is actually failing, so section 8 evaluates them separately.

**The constraint that rules out the obvious approach.** GLiNER takes entity *types* as input text, separated by `[ENT]` tokens, and was trained with the number of types capped: "we limit the number of entity types to 25 per sentence" ([GLiNER, Zaratiana et al., NAACL 2024](https://arxiv.org/abs/2311.08526)). This is not just a paper default. The shipped [`gliner_medium-v2.1` config](https://huggingface.co/urchade/gliner_medium-v2.1/raw/main/gliner_config.json) carries `"max_types": 25` and `"max_len": 384`, so the entity type strings compete with the document text for a 384 token budget. The uni-encoder attends jointly over text and types at `O((n+m)^2)` cost, which is the stated reason a bi-encoder was needed at all ([Million-Label NER](https://arxiv.org/html/2602.18487v1)). Passing 300 term strings as 300 entity types is roughly twelve times outside the training regime and would not fit the window regardless. So the workable configuration is a **single type** (`defined term`), with linking back to the vocabulary done by string match afterwards. That is the configuration section 8 tests, and it means GLiNER's headline capability, arbitrary entity types at zero shot, buys nothing here.

## 1. Precision on multi-word capitalised terms

**Zero shot span extraction is not close to the matcher, and the gap is not marginal.** GLiNER's own out of distribution average across seven benchmarks is F1 52.7 (S, 50M), 55.4 (M, 90M), 60.9 (L, 0.3B backbone) ([paper](https://arxiv.org/abs/2311.08526), Table). [GLiNER2](https://arxiv.org/html/2507.18546v1) scores 0.590 on CrossNER against GPT-4o's 0.599. The [bi-encoder](https://arxiv.org/html/2602.18487v1) reports 61.5 micro F1 on CrossNER as state of the art. A case sensitive exact match against a closed, authoritative gazetteer is a fundamentally different precision regime: its errors are confined to genuinely ambiguous capitals, which is why the design routes exactly those to review. Nothing in the zero shot literature suggests a drop-in replacement is available at any size.

**GLiNER's span representation is the shallow kind that the literature specifically documents as weak on long spans.** The paper computes `S_ij = FFN(h_i ⊗ h_j)`, a two layer feedforward network over the concatenated start and end token representations. [DSpERT (Findings of ACL 2023)](https://ar5iv.labs.arxiv.org/html/2210.04182) defines exactly this as shallow, "integrating the starting and ending tokens", and reports that on ACE 2004 a deep span representation "outperforms its shallow counterpart by 2%-12% absolute F1 score on spans shorter than 10, while this difference exceeds 30% for spans longer than 10". Multi-word defined terms are precisely the long span case. This is the single most relevant published result on axis 1, and it points against the GLiNER family rather than for it.

**There is a hard length ceiling, and it ships in the weights.** GLiNER bounds candidate span length at K=12 tokens, and `"max_width": 12` is present in the released config of both `gliner_medium-v2.1` and `gliner_large-v2.1`. Any defined term longer than 12 sub-word tokens is unreachable by construction, not merely hard, and sub-word tokenisation makes 12 tokens fewer words than it sounds. **Unverified for this document:** I did not have the extracted vocabulary available, so the longest declared term in RM6116 is unmeasured. This is a one line check against the declared list and should be run before any benchmark, because if terms exceed the ceiling it has to be raised at fine-tune time.

## 2. Nested and overlapping spans

**A BIO token classifier cannot represent the nesting at all.** One label per token means `Call-Off Contract` and `Contract` cannot both be tagged. For this pipeline that is not a defect, because the spec already forbids overlaps and mandates longest match. It does mean a token classifier can only ever learn the convention, never express the alternative, so it cannot surface the nesting as a decision for review.

**GLiNER can express nesting, but resolves it by confidence rather than by length.** The paper gives two decoders: flat, which "chooses the highest-scoring non-overlapping span", and nested, which "allows selection of fully nested spans within other entities while still avoiding partial overlaps". The implementation confirms the selection rule is score ordered, `sorted(spans, key=lambda x: -x.score)`, switching between `has_overlapping` and `has_overlapping_nested` on the `flat_ner` flag ([decoder.py](https://github.com/urchade/GLiNER/blob/main/gliner/decoding/decoder.py)).

**That is a real regression against the current rule, and it is worth naming.** DESIGN.md picks longest match on a stated principle: the longer string is always the more specific term, and a shorter term inside a longer match is a fragment, not an independent use. Greedy-by-score replaces a deterministic guarantee with a learned preference. `Call-Off Contract` beats `Contract` only when the model happens to be more confident, and there is no published guarantee that it always is. Any deployment would have to re-impose longest match as a post-processing rule, at which point the model is contributing candidate spans and the deterministic rule is still doing the arbitration.

## 3. Training data volume needed to beat the matcher

Three published curves bear on this. All are macro F1 across many entity types, so they are an optimistic proxy for a single regular type and a pessimistic one for a type whose surface forms are a fixed list.

| Source | Setting | Result |
|---|---|---|
| [GLiNER](https://arxiv.org/abs/2311.08526) Fig. 5 | 100 samples per dataset | Pile-NER pretraining worth +5.6 F1 over no pretraining, narrowing to negligible at full data |
| [NuNER](https://ar5iv.labs.arxiv.org/html/2402.15343) | k examples per entity type | k=1: 39.4, k=4: 59.6, k=8: 64.8, k=16: 67.8, k=64: 71.5 (RoBERTa baseline 24.5 / 44.7 / 52.6 / 58.1 / 65.4). **Approximate**, read from a figure |
| [GLiNER-BioMed](https://arxiv.org/html/2504.00676v1) | few shot, bi-large | 10-shot 70.39, 20-shot 73.07, 50-shot 76.02, full dataset 84.91 |

**The honest reading is that no published curve reaches the matcher's precision regime at the label volumes this repo will have.** Tens of labels lands near the k=8 to k=16 region, which is F1 in the 60s. Hundreds of labels lands near k=64, F1 around 71. Even GLiNER-BioMed's full-data 84.91 is the product of a well resourced domain adaptation: 115,000 passages and 2.3M entity mentions of LLM generated annotation, plus a two stage distillation pipeline. Its zero shot gain over the strongest general baseline was +5.96 points, 53.81 to 59.77, which is the realistic size of a domain adaptation win and is still far below where this task starts.

**So the answer to "how much data" is: more than this repo will plausibly have, for the replacement framing.** Low thousands of labelled spans is the honest floor for a span extractor to compete on precision, and the target it must beat is a baseline whose errors are already routed to a human. That conclusion is what motivates the recall-net framing in section 8 rather than a replacement bake-off.

**The counterweight, which is also modest.** Gazetteer features concatenated into a neural tagger gave +0.52 F1 on English OntoNotes at p<0.001 in [Chan et al.](https://ar5iv.labs.arxiv.org/html/2003.03072), with the finding that the features "do not hurt the performance of the neural systems, but only improves it when the gazetteer has high coverage". Small, but the high coverage condition is the one case where gains concentrate, and here coverage is complete by construction because the gazetteer is the definitions schedule itself. This supports gazetteer-as-feature over gazetteer-replaced-by-model.

## 4. Inference cost and footprint

CPU only and air gapped are both satisfiable by every candidate here. None requires an external API.

| Model | Params | Licence | Notes |
|---|---|---|---|
| `urchade/gliner_medium-v2.1` | 209M | apache-2.0 | `deberta-v3-base` backbone, trained on `urchade/pile-mistral-v0.1`, 384 token window, max span 12 |
| `urchade/gliner_large-v2.1` | 459M | apache-2.0 | `deberta-v3-large` backbone, same training data and same 384 / 12 limits |
| `numind/NuNER_Zero` | 0.4B | mit | GLiNER architecture, claims +3.1% token level F1 over gliner-large-v2.1 |
| `microsoft/deberta-v3-base` | 184M (86M backbone + 98M embedding) | mit | `do_lower_case: false`, SentencePiece, case preserving |
| `answerdotai/ModernBERT-base` | 149M | apache-2.0 | 8192 context, no `do_lower_case` field, case preserving |
| `nlpaueb/legal-bert-base-uncased` | not stated on card | cc-by-sa-4.0 | uncased, see section 6 |

**The only measured CPU latency I could find is GLiNER2's**, 130ms at 5 labels rising to 208ms at 50 labels, on unstated CPU hardware ([GLiNER2](https://arxiv.org/html/2507.18546v1), 205M params, 2048 token context, Apache 2.0). At one label the low end of that range is the right expectation. **Unverified:** no primary source gives GLiNER v2.1 CPU throughput, and the bi-encoder's 130x throughput claim at 1024 labels was measured on a single H100, not a CPU, so it does not transfer to this deployment. Memory figures are arithmetic from parameter counts rather than measured: 209M parameters is roughly 840MB at fp32, 420MB at fp16, 210MB at int8.

**Footprint is not the deciding axis.** Everything on the shortlist fits comfortably in an air gapped container, and the cost difference between a 149M and a 459M encoder is not what decides this. Precision is.

## 5. Legal domain pretraining

**The flagship legal encoder is uncased, which removes the only feature this task depends on.** `nlpaueb/legal-bert-base-uncased` lowercases its input. The entire signal distinguishing a defined term from ordinary prose in this document is capitalisation, and the design's three matching rules are built on it. Feeding this task to an uncased model is not a handicap to be measured, it is the deletion of the discriminating feature. Any legal-domain arm must use a cased checkpoint or the comparison is meaningless.

**Its contract data is also the wrong jurisdiction.** The pretraining corpora are EU legislation (EURLEX), UK legislation from legislation.gov.uk, European and US court cases, and **US contracts from SEC EDGAR** ([model card](https://huggingface.co/nlpaueb/legal-bert-base-uncased)). The UK material is legislation, not contracts. RM6116 is a UK framework agreement, so the closest-matching portion of the pretraining mix is US securities filings.

**I found no published evidence that legal-domain pretraining helps token classification on contract text.** LexGLUE and the Legal-BERT evaluations are classification-heavy. This is a gap in the evidence rather than a negative result, and I am flagging it as such rather than asserting that legal pretraining does not help. **Unconfirmed:** the existence of a cased UK-contract-specific encoder. I did not find one.

## 6. Licence

**One trap, and it is in the older checkpoints.** The v1 GLiNER models were trained on [`Universal-NER/Pile-NER-type`](https://huggingface.co/datasets/Universal-NER/Pile-NER-type), which is **CC-BY-NC-4.0**, non-commercial. [`urchade/gliner_base`](https://huggingface.co/urchade/gliner_base) carries `cc-by-nc-4.0` accordingly. These cannot ship in a commercial sovereign deployment.

**The v2.1 line is clean.** It was retrained on `urchade/pile-mistral-v0.1` and both [`gliner_medium-v2.1`](https://huggingface.co/urchade/gliner_medium-v2.1) and [`gliner_large-v2.1`](https://huggingface.co/urchade/gliner_large-v2.1) state `apache-2.0`. `numind/NuNER_Zero` states `mit`. `deberta-v3-base` is `mit`; `ModernBERT-base` is `apache-2.0`. All permissive, all commercially usable, none copyleft.

**Two flags.** `nlpaueb/legal-bert-base-uncased` is **cc-by-sa-4.0**, share-alike. Share-alike on model weights has unsettled implications for derived fine-tuned weights, and for a sovereign deployment that is a legal question rather than an engineering one. Combined with the uncased problem it is enough to exclude the model. Separately, the bi-encoder paper states its released checkpoints (`gliner-bi-edge/small/base/large-v2.0`) as **CC BY 4.0**, while `knowledgator/gliner-bi-base-v1.0` states `apache-2.0` on its card. Both are permissive, but the inconsistency means **licence must be confirmed per checkpoint at download time**, not inherited from the family.

## 7. Shortlist: three to benchmark, one excluded

1. **`urchade/gliner_medium-v2.1`** ([card](https://huggingface.co/urchade/gliner_medium-v2.1)), 209M, apache-2.0. Zero shot, single label `defined term`, **no training at all**. This is the cheapest experiment in the memo and it is first because it can be run today against the existing matcher output. It answers one question: does a zero shot extractor find term uses the matcher missed?
2. **`microsoft/deberta-v3-base`** ([card](https://huggingface.co/microsoft/deberta-v3-base)), 184M, mit, case preserving. Fine-tuned as a **span classifier over matcher-proposed candidates**, not as a NER model. This is the disambiguation arm. It is also the right control, and this is confirmed rather than assumed: `gliner_medium-v2.1` declares `"model_name": "microsoft/deberta-v3-base"` in its shipped config, so arm 1 and arm 2 share an encoder exactly. The comparison therefore isolates the span-extraction pretraining objective instead of confounding it with a change of backbone.
3. **`answerdotai/ModernBERT-base`** ([card](https://huggingface.co/answerdotai/ModernBERT-base)), 149M, apache-2.0, case preserving, 8192 context. Same role as 2, swapped backbone. Earns its place on context length: GLiNER v2.1 ships with a 384 token window, and a heading or sentence-initial judgement can depend on text further away than that. Truncated context is a plausible cause of exactly the errors being disambiguated, so an arm that cannot be context-limited is worth one slot.

**Excluded, with reasons recorded: `nlpaueb/legal-bert-base-uncased`.** Uncased (deletes the discriminating feature), cc-by-sa-4.0 (share-alike, unsettled for derived weights in a sovereign deployment), and its contract pretraining is US SEC EDGAR rather than UK. Any one of these would be a caveat; together they are a reason not to spend a benchmark slot. Reinstate it only if a cased legal-domain checkpoint appears.

## 8. The deciding experiment

**The blocking problem: `golden/decisions.jsonl` cannot measure recall.** Every row in it originates from something the matcher proposed, whether routed as ambiguous or drawn by the stratified audit. A matcher's false negatives are invisible to any sample derived from that matcher's own output. So the denominator for recall does not exist in the label store, and no amount of accumulated decisions will create it. This is the finding that shapes the experiment.

**Step 1, create the missing denominator. Blind annotation, roughly 10 pages.** Stratified across the four batches in `config.py` (B1 core-terms, B2 joint-schedule-1, B3 award-form, B4 call-off-schedule-9). The reviewer marks every defined-term use in raw text **without seeing matcher output**. This is the only unbiased recall ground truth available, it is cheap, and until it exists the question "is the model better" is unanswerable rather than merely unanswered.

**Step 2, training source, kept separate by provenance.** `decisions.jsonl` holds two populations that must not be pooled. Ambiguity-routed decisions are the hard tail, deliberately non-random, useful for training the disambiguation arm and useless as an evaluation set. Stratified audit decisions (`AUDIT.confident_term_sample_size`, strata `term_word_count` / `part` / `position`) are the unbiased sample of the confident population and are the only rows fit to evaluate precision. Train on the first, evaluate on the second, never mix.

**Step 3, test split discipline. Hold out whole parts, and separately whole terms.** A random sentence split leaks badly here: `Call-Off Contract` appears throughout the pack, so a model can memorise the term string from training sentences and score highly without generalising. Two held-out splits are needed and they answer different questions. Held-out **parts** measure performance on unseen context with a known vocabulary, which is the deployment condition for this document. Held-out **terms**, where the term string never appears in training, measure whether the model generalises beyond the gazetteer, which is the only thing that would justify replacing the gazetteer.

**Step 4, the metric that decides: recall at matched precision, tested pairwise.** Precision is not tradeable here, because the design routes ambiguity to a human rather than absorbing false positives. So fix precision at the matcher's audited level and ask only whether the model recovers more true uses at that operating point. Because both systems are scored on the same spans, the correct test is **McNemar on discordant pairs**, not two independent proportions. At 80% power and alpha 0.05, detecting a 3:1 error ratio needs about 47 discordant pairs, a 2:1 ratio about 113.

**Step 5, the threshold. Switch only when both gates pass, each on its own population:**
- **Precision gate (blocking), on the held-out parts of the stratified audit sample.** The model's false positives must not exceed the matcher's, by McNemar at p<0.05. Failing this ends the comparison regardless of recall, because precision is the property the deterministic matcher exists to guarantee.
- **Recall gate (the reason to switch), on the blind-annotated pages from step 1.** The model must recover true uses the matcher missed, again by McNemar at p<0.05 on the same spans. Nothing else can measure this, since the audit sample has no false negatives in it by construction.

Both gates must clear on data the model never trained on. A model that passes precision but not recall is not an improvement, it is a slower matcher.

**A caveat on the current audit configuration, which matters for reading any result.** `AUDIT.confident_term_sample_size` is 40. At 38 correct out of 40, precision is 0.950 with a 95% Wilson interval of [0.835, 0.986], a width of 15 points. **That sample cannot adjudicate a close precision call**, and `stratified_audit_agreement_min` of 0.90 sits inside its own confidence interval. Four hundred audited spans narrow the interval to about 4 points. The pairwise McNemar design is what makes a decision possible at realistic sample sizes, but the audit sample still has to grow before it can certify a switch.

**Expected outcome, stated in advance so the experiment can falsify it.** On section 3's published curves, none of the three arms should clear the recall gate at tens or hundreds of labels. The likely useful result is the model as a **recall net running in parallel**: union its spans with the matcher's, send only the disagreements to the existing review queue, and keep the deterministic matcher and its longest-match rule as the system of record. That configuration needs no precision guarantee from the model, converts its known weakness on multi-word spans into extra review rows rather than graph pollution, and generates exactly the labels that would eventually make the replacement question answerable.

## Implications

1. **Do not plan a replacement.** Plan a recall net beside the matcher. Every axis here points the same way, and the design's own principle that a deterministic tier 2 object outranks a generated one already anticipates it.
2. **Run the zero shot GLiNER probe first.** It needs no labels, no training and no decision, and it is the cheapest available evidence about how much recall the matcher is actually leaving on the table.
3. **Annotate 10 pages blind before benchmarking anything.** Without that denominator the central question has no measurable answer, and this is the smallest piece of work that unblocks it.
4. **Use a single entity type, never 300.** GLiNER was trained with at most 25 types per sentence; the vocabulary must be re-attached by string match after extraction.
5. **Keep longest-match as arbitration whatever wins.** Greedy-by-score is a learned preference, not the deterministic guarantee the spec relies on.
6. **Check the longest declared term against GLiNER's K=12 token ceiling** before committing to the family.
7. **Confirm licence per checkpoint at download.** The v1 to v2.1 split is a genuine non-commercial trap, and family-level assumptions are unsafe.
8. **Grow the audit sample toward 400** if it is ever to certify a model switch rather than monitor for drift.

---

## What I could not verify

- The longest defined term in RM6116, against GLiNER's K=12 token span ceiling. Needs the extracted vocabulary.
- CPU latency for GLiNER v2.1 specifically. Only GLiNER2's 130-208ms range is published, on unstated hardware.
- The NuNER few-shot numbers in section 3 are read from a figure, not a table. Directionally reliable, not exact.
- Any published NER or token classification result on contract text comparing a legal-domain encoder against a general one. **Resolved in the addendum:** the evidence exists and it is thin, roughly 1 to 2 F1, mostly from the Legal-BERT authors themselves.
- The existence of a cased legal-domain encoder, or any UK-contract-specific pretrained encoder. **Resolved in the addendum:** no UK or English-law contract encoder exists in any family checked; the sole UK-specific encoder is criminal appellate case law.
- Whether CC-BY-SA-4.0 on `legal-bert-base-uncased` propagates to fine-tuned derived weights. This is a legal question, not a technical one, and it should go to counsel rather than be assumed either way.
- Parameter count for `nlpaueb/legal-bert-base-uncased`, not stated on its card. **Resolved in the addendum:** 110M per the paper.

---

## Addendum: licence and evidence sweep of the legal encoder family

A parallel sweep of the legal-domain encoders finished after the memo above was written. Everything below is from primary sources (model card frontmatter, the HF API, the papers' own tables); the orchestrator independently re-verified the casehold licence claim against the HF API before this addendum was accepted.

**Licence map, the part that decides eligibility for a commercial sovereign deployment.**

| Family | Licence (verbatim) | Verdict |
|---|---|---|
| `nlpaueb/*` (Legal-BERT, 5 checkpoints incl. a 35M small and a contracts-only variant) | `cc-by-sa-4.0` on all | Share-alike, counsel question, and all uncased |
| `casehold/legalbert`, `casehold/custom-legalbert` | **No licence field at all** (frontmatter and API both) | Excluded: no grant of rights exists |
| `pile-of-law/legalbert-large-1.7M-2` (340M) | No model licence; dataset is CC-BY-NC-SA-4.0 with the card deferring to the paper's Appendix G | Excluded: NC-encumbered, legally ambiguous |
| `lexlms/legal-roberta-base` / `-large` (124M / 355M) | `cc-by-sa-4.0` | Only family whose corpus includes UK case law (BAILII, 1.9% of tokens) |
| `ai-law-society-lab/CaseLawModernBERT-base` / `-large` (150M / 396M, 8k context) | `apache-2.0` on the cards; the paper says CC-BY-4.0, a conflict to confirm at download | US court opinions only, no contracts, no published NER eval |
| `tsantosh7/Bailii-Roberta` | `apache-2.0` | The only UK-specific encoder found: cased, but pretrained on Court of Appeal Criminal Division judgments, not contracts; essentially undocumented |

**No pretrained encoder specific to English contract law exists.** Every contract corpus in every family checked is US SEC-EDGAR; UK material, where present, is legislation or case law. The gap the memo flagged as unconfirmed is now confirmed as real.

**The evidence that legal-domain pretraining helps contract token classification is small and mostly one group's.** The Legal-BERT paper's own CONTRACTS-NER gains are 1.1 to 1.8 F1, reported only in a bar chart, on a dataset that was never released. The follow-up with real numbers ([arXiv 2101.04355](https://arxiv.org/pdf/2101.04355), Table 5) shows LEGALBERT-CRF beating BERT-CRF in two of three subsets by 1 to 2 F1, while its own conclusion is that a BiLSTM-CRF over frozen in-domain word2vec is competitive with every BERT variant tested. The one independent ablation, CUAD (NeurIPS 2021), found 8GB of contract pretraining worth +2.6 AUPR against +5.6 for simply moving to a larger general encoder, concluding annotation quality dominates. At LegalLens-2024, legal pretraining was worth +0.7 to +2.3 macro-F1 while pipeline and architecture choices were worth about 32. LexGLUE contains zero token-level tasks, so it cannot speak to this question either way.

**Consequences for the shortlist above: none.** The exclusion of `legal-bert-base-uncased` stands on stronger ground (the whole family is uncased or licence-blocked, and the domain gain it would be chasing is 1 to 2 F1 at best). The three-arm experiment is unchanged. If a legal-domain arm is ever added despite this, `lexlms/legal-roberta-base` is the least-bad candidate (cased, UK case law present, share-alike caveat), and `casehold/*` and `pile-of-law/*` must not ship regardless of merit.
