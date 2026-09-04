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
