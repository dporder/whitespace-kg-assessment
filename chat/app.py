"""Chat UI. FastAPI plus one static page, streaming.

    .venv/bin/uvicorn chat.app:app --port 8001        (from the repo root)

Endpoints
    GET  /                     the one page
    GET  /api/health           which backends are live
    GET  /api/ask?q=...        SSE: gate, plan, tool, text, citations, done
    GET  /api/tool/{name}      run one tool directly, for inspection and curl
    GET  /api/crop?path=...    PNG crop for a node, rendered from its bbox
    GET  /api/crop?page=&bbox= PNG crop for an explicit box
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import config as ui_config
from . import crops
from .agent import describe_backend, run_turn
from .backends import get_backend
from .backends.base import TOOL_NAMES
from .tools import ToolRunner

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="RM6116 chat", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    info = describe_backend()
    info["tools"] = list(TOOL_NAMES)
    return info


# --------------------------------------------------------------------------
# the streaming turn
# --------------------------------------------------------------------------
def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/ask")
def ask(request: Request, q: str = Query(..., min_length=1, max_length=2000)) -> StreamingResponse:
    def gen():
        yield _sse("open", {"question": q, **describe_backend()})
        try:
            for event, payload in run_turn(q):
                yield _sse(event, payload)
        except Exception as exc:                       # never leave the stream hanging
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        yield _sse("close", {})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# direct tool access, for inspection
# --------------------------------------------------------------------------
@app.get("/api/tool/{name}")
def run_tool(name: str, request: Request) -> JSONResponse:
    if name not in TOOL_NAMES:
        raise HTTPException(404, f"no such tool {name!r}; the tools are {', '.join(TOOL_NAMES)}")
    args = dict(request.query_params)
    if "limit" in args:
        try:
            args["limit"] = int(args["limit"])
        except ValueError:
            raise HTTPException(400, "limit must be an integer")
    runner = ToolRunner()
    call = runner.run(name, args)
    if not call.ok:
        raise HTTPException(400, call.error or "tool failed")
    return JSONResponse(
        {
            "tool": name,
            "args": args,
            "summary": call.summary,
            "ms": call.ms,
            "result": call.result,
        }
    )


# --------------------------------------------------------------------------
# crops
# --------------------------------------------------------------------------
def _png(data: bytes) -> Response:
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/crop")
def crop(
    path: str | None = None,
    page: int | None = None,
    bbox: str | None = None,
    colour: str = crops.DEFAULT_COLOUR,
    width: int = Query(crops.TARGET_WIDTH, ge=120, le=2400),
    zoom: float | None = Query(None, ge=0.5, le=8.0),
) -> Response:
    """By node path, or by explicit page and bbox. Rendered at request time."""
    if path:
        out = get_backend().cite(path)
        if not out.get("found"):
            raise HTTPException(404, out.get("reason", f"no crop for {path!r}"))
        return _png(out["png"])

    if page is None or not bbox:
        raise HTTPException(400, "give either path, or both page and bbox")
    try:
        box = [float(v) for v in bbox.split(",")]
    except ValueError:
        raise HTTPException(400, "bbox must be four comma-separated numbers")
    if len(box) != 4:
        raise HTTPException(400, "bbox must be four comma-separated numbers")
    try:
        return _png(crops.render_crop(page, box, colour=colour, width=width, zoom=zoom))
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/answer_graph")
def answer_graph(paths: str = Query(..., min_length=1)) -> dict:
    """The provisions behind an answer and how they point at one another.

    Assembled only from get_provision and follow_references, labels included:
    both tools report the name the agreement uses, so SPEC 6's rule that the
    tools are the only data access holds for this endpoint with no exception.
    """
    wanted = [p.strip() for p in paths.split(",") if p.strip()][:12]
    if not wanted:
        raise HTTPException(400, "give at least one path")

    runner = ToolRunner()
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add(path: str, state: str, footnote: int | None = None,
            fallback_name: str | None = None) -> dict:
        if path in nodes:
            if footnote and nodes[path]["footnote"] is None:
                nodes[path]["footnote"] = footnote
            return nodes[path]
        got = runner.run("get_provision", {"path": path}).result
        found = bool(got.get("found"))
        nodes[path] = {
            "id": path,
            # tool-reported name; for a target outside the corpus the citing
            # ref supplied one, and only then do we fall back to the raw path
            "label": got.get("name") or runner.ledger.names.get(path) or fallback_name or path,
            "kind": got.get("kind"),
            "page": (got.get("page") or {}).get("start") if found else None,
            "footnote": footnote,
            "state": state if (found or state == "external") else "unsettled",
            "loaded": found,
        }
        return nodes[path]

    for i, path in enumerate(wanted, start=1):
        add(path, "primary" if i == 1 else "normal", footnote=i)

    for path in list(nodes):
        if not nodes[path]["loaded"]:
            continue
        refs = runner.run("follow_references", {"path": path, "direction": "outbound"}).result
        for r in refs.get("references", []):
            target, status = r.get("target_path"), r.get("status")
            if target:
                # An Act resolved to a legislation key is settled, not doubtful:
                # it simply lives outside this agreement, so it gets its own state.
                external = status == "external"
                add(target, "external" if external else "normal",
                    fallback_name=r.get("target_name"))
                edges.append({"from": path, "to": target, "label": "points at",
                              "state": "external" if external else "settled"})
            elif status in ("ambiguous", "unresolved"):
                key = f"unsettled:{r.get('ref_path')}"
                nodes.setdefault(key, {
                    "id": key,
                    "label": f"“{r.get('text')}”",
                    "kind": "ref", "page": r.get("page"), "footnote": None,
                    "state": "unsettled", "loaded": False,
                    "note": "more than one match" if status == "ambiguous"
                            else "not in this document set yet",
                })
                edges.append({"from": path, "to": key, "label": "points at",
                              "state": "unsettled"})

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "tool_calls": len(runner.calls),
    }


@app.get("/api/config")
def config_view() -> dict:
    return {
        "DATA_SOURCE": ui_config.DATA_SOURCE,
        "GRAPH_BACKEND": ui_config.GRAPH_BACKEND,
        "EMBEDDING_SEARCH": ui_config.EMBEDDING_SEARCH,
        "MAX_TOOL_ROUNDS": ui_config.MAX_TOOL_ROUNDS,
        "MAX_TOOL_CALLS": ui_config.MAX_TOOL_CALLS,
        "data_root": str(ui_config.data_root()),
    }
