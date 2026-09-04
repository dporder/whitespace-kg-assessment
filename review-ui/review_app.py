"""Review UI. FastAPI plus one static page.

    .venv/bin/uvicorn review_app:app --port 8000 --app-dir review-ui

`review-ui` has a hyphen so it cannot be a package; --app-dir puts it on the
path and the modules carry a review_ prefix so they never collide with chat/.

Endpoints
    GET  /                      the one page
    GET  /api/queue             the rows, filterable by kind and part
    GET  /api/status            which data root and PDF are live
    GET  /api/decisions         what has been decided so far
    POST /api/decisions         append one verdict to golden/decisions.jsonl
    GET  /api/crop?page=&bbox=  PNG crop rendered from the box at request time
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import review_data                        # noqa: E402
import review_decisions                   # noqa: E402
from chat import crops                    # noqa: E402

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="RM6116 review queue", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status() -> dict:
    info = review_data.source_info()
    info["decisions"] = review_decisions.summary()
    return info


@app.get("/api/queue")
def queue(
    kinds: str = Query("ref,term,anomaly", description="comma-separated: ref, term, anomaly"),
    part: str | None = None,
    include_decided: bool = True,
) -> JSONResponse:
    wanted = tuple(k.strip() for k in kinds.split(",") if k.strip())
    bad = [k for k in wanted if k not in ("ref", "term", "anomaly")]
    if bad:
        raise HTTPException(400, f"unknown kind(s) {bad}; use ref, term, anomaly")
    rows = review_data.queue(kinds=wanted, part=part, include_decided=include_decided)
    return JSONResponse({"counts": review_data.counts(rows), "rows": rows})


@app.get("/api/decisions")
def decisions() -> dict:
    return review_decisions.summary()


@app.post("/api/decisions")
def add_decision(decision: dict = Body(...)) -> JSONResponse:
    # No "unknown" fallback: a label whose author is not recorded is not
    # ground truth anybody can audit later, so it is refused outright.
    try:
        stored = review_decisions.append(decision)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse(
        {"stored": stored, "target": review_decisions.target_key(stored),
         "file": str(review_decisions.path())},
        status_code=201,
    )


@app.get("/api/crop")
def crop(
    page: int = Query(..., ge=1),
    bbox: str = Query(..., description="x0,y0,x1,y1 in PyMuPDF points"),
    colour: str = crops.DEFAULT_COLOUR,
    width: int = Query(crops.TARGET_WIDTH, ge=120, le=2400),
    zoom: float | None = Query(None, ge=0.5, le=8.0),
) -> Response:
    try:
        box = [float(v) for v in bbox.split(",")]
    except ValueError:
        raise HTTPException(400, "bbox must be four comma-separated numbers")
    if len(box) != 4:
        raise HTTPException(400, "bbox must be four comma-separated numbers")
    try:
        png = crops.render_crop(page, box, colour=colour, width=width, zoom=zoom)
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})
