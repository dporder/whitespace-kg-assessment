# `golden/decisions.jsonl`, the label record contract

The review UI appends here, stage 8 reads. Both sides agree on this file and
nothing else, so it is deliberately small. Owned by eval-builder (the consumer);
the review UI writes it, `pipeline/eval/golden.py` is the reference reader.

One JSON object per line, UTF-8, append only. Never rewritten, never sorted, no
deletions: a reviewer who changes their mind appends a new record for the same
subject and the harness takes the **last record per subject**. That keeps the
file an audit trail, which is the point of growing ground truth out of operating
the system (EVALUATION.md section 3).

```json
{"kind": "ref", "path": "core-terms/9/9.1/intro/ref@11-23", "verdict": "target",
 "chosen_candidate": "core-terms/3/3.1/3.1.2", "reviewer": "dan",
 "ts": "2026-09-04T21:15:03Z"}
```

## Fields

| field | required | meaning |
|---|---|---|
| `kind` | yes | `ref`, `term` or `anomaly`. Chooses the verdict vocabulary below. |
| `path` | one of | The subject. For a ref this is the ref's own path, `<parent-path>/ref@<start>-<end>`, which already carries the span. For an outline triage item it is the queue id printed in the report. |
| `node_id` + `char_span` | one of | The subject as a span of a node's text, when a path is not to hand. `char_span` is `[start, end]`, offsets into that node's `text` (its `title` for a heading match), exactly as `TermUse.char_span` in `schemas.py`. |
| `verdict` | yes | See the vocabularies. An unrecognised verdict is counted and reported, never silently dropped. |
| `chosen_candidate` | **yes** for `ref`/`target` and `term`/`use` | The correct target path, or the governing term. See below: a record that omits it where it is required is malformed and is never scored. |
| `anomaly_index` | **yes** for `anomaly`/`confirmed` and `anomaly`/`rejected` | Which of the node's `anomalies` the verdict is about, 0-based. Part of the subject key. |
| `reviewer` | yes | Who decided. Free text. |
| `ts` | yes | ISO 8601 UTC. Ordering within a file is the file's own order; `ts` is for provenance and for the sampling frame, not for sorting. |
| `note` | no | Free text for a human. Never parsed. |

Unknown extra keys are ignored by the reader, so the review UI may carry its own
bookkeeping without breaking the harness.

### Why `chosen_candidate` is required, not merely expected

A verdict of `target` or `use` asserts what the right answer *is*. Without it the
harness has only bad options, and it took a different bad option for each kind
before this was enforced: for refs it compared the pipeline's target against
`None` and scored a **label defect as a parser failure**; for terms it fell back
to the pipeline's own answer and **graded the pipeline against itself**, which is
silently wrong exactly where it matters most, on alias collisions. Refusing the
record is better than either. A missing `chosen_candidate` therefore joins the
malformed-lines count, is reported with its file and line, and never reaches a
rate.

`unresolvable`, `not_a_reference` and `not_a_use` assert that there is no right
answer to name, so they need no `chosen_candidate`.

### Why `anomaly_index` is part of the subject

A node can carry several anomalies and a verdict on one is not a verdict on the
others. Without the index in the subject key the second decision on a node
silently replaced the first and one reviewer's work was lost. It is required for
the node-anomaly verdicts (`confirmed`, `rejected`).

The triage verdicts (`agree`, `parser_wrong`, `outline_wrong`, `both_differ`) do
not carry one: their subject is the `outline_vs_provided` queue id, which names a
disagreement between two descriptions of the document, not an anomaly recorded
on a node. There is no index for them to carry. This is the one place where this
document is narrower than SPEC section 6's sentence "Anomaly records carry an
`anomaly_index`", and it is a narrowing of scope rather than a contradiction of
the vocabulary.

## Verdicts, `kind: "ref"`

| verdict | means | scored as |
|---|---|---|
| `target` | It is a citation, and the correct target is `chosen_candidate` (**required**). | Detection true positive. Resolution correct iff the pipeline's `target_path` equals `chosen_candidate`. |
| `unresolvable` | It is a citation, and no correct target exists in the corpus (or none can be determined). Correct behaviour is abstention: `unresolved` or `ambiguous` with no `target_path`. | Detection true positive. Abstention correct iff the pipeline did **not** resolve it. Any resolved one trips the zero-tolerance gate `wrongly_resolved_unresolvables_max`. |
| `not_a_reference` | The span is not a citation at all. | Detection false positive if the pipeline emitted a ref there. |

Detection recall counts the `target` and `unresolvable` records whose span the
pipeline found. Resolution precision counts only `target` records the pipeline
actually resolved. They are never combined into one number: SPEC 2.6 requires
them reported separately, because conflating them hides which half is broken.

## Verdicts, `kind: "term"`

| verdict | means | scored as |
|---|---|---|
| `use` | The span is a use of a defined term. `chosen_candidate` names the **governing** term, which may differ from the one the pipeline matched (alias collisions), and is required on every `use` record, including ones where the pipeline agrees. | Detection true positive when the pipeline matched the same term. Wrong term = a false positive for the term the pipeline chose and a false negative for the term named. |
| `not_a_use` | The span is capitalised but is not a use of a defined term. | False positive if the pipeline emitted a `USES_TERM` there. |

False positives and false negatives are broken out by `ambiguity_kind`. For a
false positive the kind comes from the pipeline's own `TermUse` record. For a
false negative there is no pipeline record, so the harness derives the kind
deterministically (sentence initial, heading, else none) and marks the row
`ambiguity_kind_source: eval_derived` rather than pretending the pipeline said it.

## Verdicts, `kind: "anomaly"`

| verdict | subject | means |
|---|---|---|
| `agree` | queue id | The derived tree and the provided outline say the same thing after all. |
| `parser_wrong` | queue id | The derived tree is wrong here. |
| `outline_wrong` | queue id | The embedded outline is wrong here. |
| `both_differ` | queue id | Both disagree with the page; neither is right. |
| `confirmed` | node + `anomaly_index` | A node anomaly's proposed reading was accepted. |
| `rejected` | node + `anomaly_index` | A node anomaly's proposed reading was rejected. |

The verdict is `rejected`, not `reject`: these are past-tense outcomes
throughout.

The first four are the `outline_vs_provided` triage. Their `path` is the queue
id the report prints for the disagreement: the derived node's path when there is
one, otherwise `outline:<part>#<outline entry index>`. Anything in the triage
queue with no record is counted **unreviewed** and printed as such. The harness
never rules for either side on its own.

## What the harness does not assume

Scale. Ten labels and ten thousand behave identically: every rate is printed
with its absolute counts, and a rate over an empty denominator is `null`, not
zero and not one. An empty or missing `golden/` degrades the golden sections to
`no_data` and marks their gates `skipped_no_data`; it never fails a gate and
never reports a perfect score.
