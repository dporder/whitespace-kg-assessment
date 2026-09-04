# Chat UI

FastAPI plus one static page, streaming. Three pieces from SPEC 6, kept deliberately small.

```
.venv/bin/uvicorn chat.app:app --port 8001     # from the repo root
```

## The three pieces

**1. A gate.** A cheap classifier (`config.MODELS["chat_gate"]`) decides plain lookup or research. It fails open: any error, any unexpected answer, anything ambiguous routes to research. Verified by `tests/chat/test_agent_loop.py`.

**2. A planning step.** Before any tool runs, the question is restated as focused sub-queries grouped into batches, where a batch is a set that can run at once and a later batch exists only because it genuinely needs an earlier one's result. That structure is what streams to the user as "working on".

**3. A bounded tool loop.** Exactly the seven tools below, bounded by `MAX_TOOL_ROUNDS` and `MAX_TOOL_CALLS`. A model that only ever calls tools still terminates.

## The tools are the only data access

There is no side channel to the trees, the graph or the PDF, so every claim traces to a call in the transcript.

| tool | returns |
|---|---|
| `find_provision(query)` | fuzzy (rapidfuzz) over paths, numbers, titles and terms; hits carry a path and a page, never text. The embedding arm sits behind `EMBEDDING_SEARCH` and reports `vector index pending` rather than pretending, because stage 6 has not run |
| `get_provision(path)` | derived text (children walked in `order`, never stored — SPEC 2.1), children, page, boxes, anomalies. The only source of quotable text |
| `follow_references(path, direction)` | outbound refs anchored anywhere in the subtree, or inbound refs resolving to this node, each with status and candidates |
| `define(term)` | definition text, source, aliases, and `governs` — which site governs in each part, part-local shadowing document |
| `find_by_concept(label)` | the concept neighbourhood. `citable: false`, always: cite the member provisions |
| `history(lineage_key)` | the version chain. Wired, and honest that one version is loaded |
| `cite(path)` | the page-image crop, rendered from the stored box |

`cite` returns PNG bytes from the backend, but the model never sees them — the tool layer swaps the bytes for a page, a box and a `crop_url`, and the browser fetches the image. Bytes in the transcript would cost a fortune and prove nothing.

## Citations are checked, not requested

The model writes citations as `[[path|page]]`. Every path and page a tool returns is recorded in a `CitationLedger`, and each citation in the finished answer is looked up in it:

- `ok` — the tools returned this exact path at this exact page. Only this offers a crop.
- `page_mismatch` — real path, wrong page.
- `page_unparseable` — the page is missing or not an integer (`[[path|]]`, `[[path|p2]]`). It cannot be checked against anything, so it fails; treating it as ok was a real hole that let both of those render as verified.
- `unknown_path` — no tool ever returned it.

Anything but `ok` is marked amber in the page, refuses to open a crop, and is listed under the answer. An answer with **no** citations gets a warning, not a green banner: nothing in it is backed by the graph.

So an invented citation is detectable rather than merely discouraged.

## Two switches

`chat/config.py`:

- `DATA_SOURCE` — `"fixtures"` or `"output"`. Moves both UIs onto real pipeline output.
- `GRAPH_BACKEND` — `"fixtures"` (JSON files), `"neo4j"` (read-only parameterised Cypher), or `"auto"` (Neo4j when the graph exists, otherwise the files). Same tool contract either way.

The Neo4j backend's Cypher is module-level constants with `$parameters`; `tests/chat/test_crops_and_backends.py` parses the module's AST to assert every query is a plain string literal, so no caller input can reach the database as syntax, and greps each for write clauses.

## LLM access

`chat/llm_client.py` is the one seam. It imports `pipeline/llm.py` and uses it when present; otherwise it takes an **interim path** straight to the anthropic SDK with the same call-logging shape SPEC ground rule 0 asks for (model, prompt version and raw response under `output/<run>/llm_log/`). No other module in `chat/` imports anthropic, so deleting the interim path is confined to that file.

Keys come from the environment or the gitignored `.env` at `config.ENV_FILE` and are never logged or returned. If the key is identity-linked, the API also needs the workspace it acts in: set `ANTHROPIC_WORKSPACE_ID` in either place and it is sent as the `anthropic-workspace-id` header, no code change.

## Scripted mode, for demonstrating without a model

The key in this environment is identity-linked and the workspace id it needs is not available, so a live exchange cannot run here. Rather than have no demo, opt in:

```
CHAT_SCRIPTED=1 .venv/bin/uvicorn chat.app:app --port 8001
```

The gate, the planner, the bounded tool loop, every tool call, the citation ledger and every crop are the **real** code paths over the fixtures backend. Only the model's turns are canned, in `chat/scripted.py`. It is off unless the variable is explicitly truthy, `/api/health` reports `scripted: true`, and the page shows a **"scripted model — canned turns, real tools"** badge, so nobody can mistake it for a live answer.

It is not a bypass of the citation rules: the canned answer's citations go through the same ledger as any other, and while writing it the checker rejected a clause the script had not actually fetched, which is why the script now reads it. A question with no script says so and cites nothing rather than inventing.

## Endpoints

| method | path | does |
|---|---|---|
| GET | `/` | the page |
| GET | `/api/health` | which backends are live, and the tool list |
| GET | `/api/config` | the two switches and the resolved data root |
| GET | `/api/ask?q=` | SSE: `gate`, `plan`, `tool`, `text`, `citations`, `done` |
| GET | `/api/tool/{name}?...` | run one tool directly, for inspection |
| GET | `/api/crop?path=` | PNG crop for a node |

## Tests

```
.venv/bin/python -m pytest tests/chat -q
```

The model is scripted in tests rather than called, so the suite pins our loop, our bounds and our citation checking without a network round trip.
