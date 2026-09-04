# Review UI

FastAPI plus one static page. Lists everything the pipeline could not settle on its own — ambiguous and unresolved refs, ambiguous term uses, and anomalies with a proposed reading — and turns a reviewer's verdict into a golden label.

```
.venv/bin/uvicorn review_app:app --port 8000 --app-dir review-ui     # from the repo root
```

`review-ui` contains a hyphen, so it is not an importable package. `--app-dir` puts it on the path and its modules carry a `review_` prefix so they never collide with `chat/`.

## Where the data comes from

One value switches both UIs between hand-made fixtures and real pipeline output: `DATA_SOURCE` in `chat/config.py`. `"fixtures"` reads `fixtures/`, `"output"` reads `output/<run>/`, newest run unless `OUTPUT_RUN` names one. Nothing else changes.

`chat/source.py` (loading and indexing) and `chat/crops.py` (crop rendering) serve both UIs. They live under `chat/` only because it is the one of ui-builder's two directories that is a legal Python package, and the SPEC section 1 ownership map provides no third. `review_data.py` is the single import site, so relocating them is a one-line change.

## What lands in the queue

| kind | lands when | shows |
|---|---|---|
| `ref` | `status` is `ambiguous` or `unresolved` | the citing sentence with the pointing words highlighted, the candidates with scores and reasons, the resolver and its confidence |
| `term` | a `TermUse` has `status: ambiguous` | the sentence with the matched span highlighted, the ambiguity kind, and the definition that governs in that part |
| `anomaly` | a node carries anything in `anomalies` | the text exactly as printed, and the proposed reading where the anomaly names one |

Resolved and external refs never appear: a settled ref does not ask for review.

Each row carries a crop rendered server-side from the node's stored bbox by PyMuPDF **at request time**. Nothing is pre-baked and no page image is ever written into the repo — SPEC ground rule 0 treats the PDF as read-only and forbids copying document content anywhere but `output/`.

The box is drawn in the trust-gradient colour of the tier that proposed the row, matching `diagram/Main.dc.html`: blue `#3f6396` deterministic, teal `#2e7373` rule-derived, amber `#a16b16` model-derived, purple `#75589b` human. A row's left edge tells you who proposed it; every control that records a human verdict is purple.

> While `DATA_SOURCE` is `"fixtures"`, crops show real ink from the real PDF at fabricated coordinates, so the image will not match the synthetic fixture sentence beside it. `fixtures/README.md` explains why. The crop path is truthful; only the coordinates are invented, and they become real the moment `output/` trees land.

## Anomalies and proposed readings

`schemas.py` types `anomalies` as a list of plain strings, with no field for a model-proposed reading or its confidence, though SPEC 2.1 says an anomaly "may carry a model proposed reading with a confidence". Until that field exists, `review_data.parse_anomaly` reads the `<code>: <detail>` convention the fixtures and the SPEC examples use, and where the detail says `'X' for 'Y'` it offers `Y` as the correction:

- `found_token` / `proposed_token` — quoted verbatim out of the anomaly string.
- `proposed` — the node's own text with that one substitution applied, which is what the reviewer actually judges. Offered **only** when the token really occurs in the text, so no reading is invented.

The stored text is never altered, whichever way the verdict goes. This is a place worth revisiting when the schema grows a typed field.

## golden/decisions.jsonl

**This vocabulary is not ours.** It is the contract seam between this UI and the eval harness, pinned in SPEC section 6 and specified in `pipeline/eval/GOLDEN_FORMAT.md`, whose reference reader is `pipeline/eval/golden.py`. That harness is the consumer; this UI writes what it loads. What follows summarises the contract — where the two disagree, the harness wins, and `tests/review_ui/test_decisions.py` compares our verdict tables against the reader's own rather than restating them, so they cannot drift apart silently again.

One JSON object per line, appended under a lock, never rewritten. Written to `config.GOLDEN / "decisions.jsonl"` unless `RM6116_DECISIONS_PATH` overrides it; the directory is created on first write.

Every record carries `kind`, `verdict`, `reviewer` (non-empty — there is no "unknown" fallback, a label nobody signed is not auditable) and `ts` (ISO 8601 UTC).

### Verdicts

| kind | verdict | means | `chosen_candidate` |
|---|---|---|---|
| `ref` | `target` | it is a citation and this is the right target | **required** — the accepted target path |
| `ref` | `unresolvable` | a real citation with no correct target in the corpus | refused |
| `ref` | `not_a_reference` | the span is not a citation at all | refused |
| `term` | `use` | the span is a use of a defined term | **required** — the *governing* term, which may differ from the matched one in an alias collision |
| `term` | `not_a_use` | capitalised, but not a use of a defined term | refused |
| `anomaly` | `confirmed` / `rejected` | the proposed reading was accepted or rejected | refused |
| `anomaly` | `agree` / `parser_wrong` / `outline_wrong` / `both_differ` | the `outline_vs_provided` triage set | refused |

`unresolvable` is the label the zero-tolerance abstention gate feeds on, so the UI gives it its own control rather than folding it into a rejection.

### Subject identity

The harness keeps the **last record per subject**, where the subject is `(kind, path, node_id, span)`:

| kind | subject fields | row id |
|---|---|---|
| `ref` | `path` — the ref's own path, `<parent>/ref@<start>-<end>`; the reader recovers the span from it | the ref path |
| `term` | `node_id` + `char_span` `[start, end]` | `<node_id>:<start>-<end>` |
| `anomaly` | `node_id` + **`anomaly_index`** (int) | `<node_id>#<index>` |

`anomaly_index` is load-bearing, not bookkeeping: a node can carry several anomalies, and without the index two verdicts on one node supersede each other and one is silently lost. It is required here and `test_two_anomalies_on_one_node_are_separate_subjects` pins it.

Worked examples, exactly as written:

```json
{"chosen_candidate": "framework-schedule-2", "kind": "ref", "path": "core-terms/9/9.2/ref@111-121", "reviewer": "dan", "ts": "2026-09-04T05:52:11Z", "verdict": "target"}
{"kind": "ref", "path": "core-terms/9/9.1/intro/ref@28-71", "reviewer": "dan", "ts": "2026-09-04T05:52:14Z", "verdict": "unresolvable"}
{"char_span": [0, 21], "chosen_candidate": "Good Working Practice", "kind": "term", "node_id": "3effa77976523486d1978c32e8a71224a163e317", "path": "core-terms/3/3.1/3.1.1/b", "reviewer": "dan", "ts": "2026-09-04T05:52:18Z", "verdict": "use"}
{"anomaly": "stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim", "anomaly_index": 0, "kind": "anomaly", "node_id": "120550221b2062ffe7fe7cd61431217257332840", "path": "award-form/3/label", "reviewer": "dan", "ts": "2026-09-04T05:52:22Z", "verdict": "confirmed"}
```

Extra keys (`path` on a term or anomaly row, for human legibility) are ignored by the reader.

- Append-only, so **later lines win**. `decisions_by_target()` returns the latest verdict per row, which is how a reviewer corrects a mistake.
- A malformed record is refused with a 400 and **nothing is written**, so the file never needs repairing.
- **Demo and test flows write only to temporary paths** (SPEC section 6), via `RM6116_DECISIONS_PATH`, so a `decisions.jsonl` inside the repo always holds real reviewer verdicts.

## Endpoints

| method | path | does |
|---|---|---|
| GET | `/` | the page |
| GET | `/api/status` | data root, PDF presence, parts, and the decisions summary |
| GET | `/api/queue?kinds=&part=&include_decided=` | rows plus counts |
| GET | `/api/decisions` | count, breakdown, five most recent |
| POST | `/api/decisions` | validate and append one verdict; 201 with the stored record, 400 with the reason |
| GET | `/api/crop?page=&bbox=&colour=&zoom=` | PNG, rendered at request time |

## Controls, one per verdict

Every verdict in the vocabulary is reachable from the row it applies to; none is inferred from the absence of another.

| row | controls |
|---|---|
| `ref` | **Confirm target** (`A`, enabled only when the pipeline resolved one) · **Pick candidate** (`1`–`9` to select, then the button) · **Unresolvable** (`U`) · **Not a reference** (`N`) |
| `term` | a **governing-term picker** defaulting to the matched term, then **Real use** (`A`) or **Not a use** (`N`) |
| `anomaly` | **Confirm** (`A`) · **Reject** (`N`) |

Confirm target and Pick candidate both write `target`; they are separate controls because accepting the pipeline's answer and overriding it are different reviewer acts, and only one of them is available on any given row.

`J`/`K` move. The direction chosen on the design canvas trades rows-per-screen for evidence that cannot be skipped, and buys the density back by keeping the reviewer off the mouse.

## Tests

```
.venv/bin/python -m pytest tests/review_ui -q
```
