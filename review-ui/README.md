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

One JSON object per line, appended under a lock, never rewritten. Stage 8 consumes it as labels. Written to `config.GOLDEN / "decisions.jsonl"`; the directory is created on first write.

Every record carries these four:

| field | type | meaning |
|---|---|---|
| `kind` | `"ref" \| "term" \| "anomaly"` | which queue the row came from |
| `verdict` | `"approve" \| "reject"` | the reviewer's judgement |
| `reviewer` | string, non-empty | who decided |
| `ts` | `YYYY-MM-DDTHH:MM:SSZ` | when, UTC |

Then, by kind:

| kind | identity | extra |
|---|---|---|
| `ref` | `path` — the ref's own path, `<parent>/ref@<start>-<end>` | `chosen_candidate`, a candidate path; only legal on an `approve` |
| `term` | `node_id` + `char_span` `[start, end]` | `term`, `path` for legibility |
| `anomaly` | `node_id` + `anomaly`, the recorded string this answers | `anomaly_index`, `path` |

`anomaly` is required because one node can carry several anomalies and `node_id` alone would not say which was judged. `path` and `term` are conveniences a consumer may ignore.

Worked examples, exactly as written:

```json
{"chosen_candidate": "framework-schedule-2", "kind": "ref", "path": "core-terms/9/9.2/ref@111-121", "reviewer": "dan", "ts": "2026-09-04T04:13:46Z", "verdict": "approve"}
{"char_span": [0, 21], "kind": "term", "node_id": "3effa77976523486d1978c32e8a71224a163e317", "path": "core-terms/3/3.1/3.1.1/b", "reviewer": "dan", "term": "Good Working Practice", "ts": "2026-09-04T04:13:46Z", "verdict": "reject"}
{"anomaly": "stray_character_in_label: 'rFramework' for 'Framework', recorded verbatim", "anomaly_index": 0, "kind": "anomaly", "node_id": "120550221b2062ffe7fe7cd61431217257332840", "reviewer": "dan", "ts": "2026-09-04T04:13:46Z", "verdict": "approve"}
```

Reading it back:

- The file is append-only, so **later lines win**. `decisions_by_target()` returns the latest verdict per row, which is how a reviewer corrects a mistake.
- `target_key(decision)` reproduces the queue row id a decision answers: the ref path, or `<node_id>:<start>-<end>`, or `<node_id>#<anomaly_index>`.
- A malformed record is refused with a 400 and **nothing is written**, so the file never needs repairing. `review_decisions.validate` is the single gate; `tests/review_ui/test_decisions.py` pins it.

What a verdict means, so the harness scores the right thing:

- **ref, approve with `chosen_candidate`** — that candidate is the correct target.
- **ref, approve with none** — the reading as it stands is right; for an `unresolved` ref that means it is genuinely unresolvable from the corpus, which is the abstention case SPEC 5 scores.
- **ref, reject** — no candidate offered is correct.
- **term, approve** — the span really is a use of the defined term. **reject** — ordinary words that happen to be capitalised.
- **anomaly, approve** — the proposed reading is right, or the anomaly is correctly recorded where there is no proposal. **reject** — it is not.

## Endpoints

| method | path | does |
|---|---|---|
| GET | `/` | the page |
| GET | `/api/status` | data root, PDF presence, parts, and the decisions summary |
| GET | `/api/queue?kinds=&part=&include_decided=` | rows plus counts |
| GET | `/api/decisions` | count, breakdown, five most recent |
| POST | `/api/decisions` | validate and append one verdict; 201 with the stored record, 400 with the reason |
| GET | `/api/crop?page=&bbox=&colour=&zoom=` | PNG, rendered at request time |

## Keyboard

`J`/`K` move, `A` approve, `X` reject, `1`–`9` pick a candidate. The direction chosen on the design canvas trades rows-per-screen for evidence that cannot be skipped, and buys the density back by keeping the reviewer off the mouse.

## Tests

```
.venv/bin/python -m pytest tests/review_ui -q
```
