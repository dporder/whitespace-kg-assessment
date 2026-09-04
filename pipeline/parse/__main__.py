"""Stage 1 CLI.

    python -m pipeline.parse --parts core-terms
    python -m pipeline.parse --parts core-terms,award-form
    python -m pipeline.parse --full-structural

Writes `output/<run>/layout/<part>.json`. Exit 0 on success, 1 on failure.
The run directory defaults to "current" and is deterministic: nothing in a path
or a file depends on the clock, so a rerun overwrites byte-identical output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config

from .document import scan
from .model import dump_json
from .layout import build_layout
from .numbering import Rulebook

DEFAULT_RUN = "current"


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", default=DEFAULT_RUN, help="run directory under output/")
    parser.add_argument(
        "--profile",
        default=None,
        help="hierarchy profile name; defaults to the one output/profile.json assigned",
    )


def load_profile() -> dict:
    """Stage 1 reads the profile stage 0 wrote. Stage 0 is not optional: a part
    that failed its fit checks is not parsed, and without the profile there is
    no verdict to honour."""
    profile_path = config.OUTPUT / "profile.json"
    if not profile_path.exists():
        raise SystemExit(
            f"parse: {profile_path.relative_to(config.ROOT)} is missing; "
            "run `python -m pipeline.profile` first"
        )
    return json.loads(profile_path.read_text(encoding="utf-8"))


def resolve_profile(name: str | None, profile_data: dict) -> Rulebook:
    if name is None:
        name = profile_data.get("profile", {}).get("assigned") or config.DEFAULT_PROFILE
    if name not in config.HIERARCHY_PROFILES:
        raise SystemExit(f"unknown hierarchy profile {name!r}")
    return Rulebook(name, config.HIERARCHY_PROFILES[name])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.parse")
    parser.add_argument(
        "--parts",
        default=None,
        help="comma-separated part ids, e.g. core-terms,award-form",
    )
    parser.add_argument(
        "--full-structural",
        action="store_true",
        help=f"every part across pages {config.FULL_STRUCTURAL_PAGES[0]} to "
             f"{config.FULL_STRUCTURAL_PAGES[1]}",
    )
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    if not args.parts and not args.full_structural:
        parser.error("give --parts or --full-structural")

    profile_data = load_profile()
    rulebook = resolve_profile(args.profile, profile_data)
    page_range = config.FULL_STRUCTURAL_PAGES if args.full_structural else None
    document = scan(config.PDF, config.BATCHES, page_range=page_range)

    if args.full_structural:
        wanted = [p.slug for p in document.parts]
    else:
        wanted = [p.strip() for p in args.parts.split(",") if p.strip()]

    missing = [w for w in wanted if document.part_by_id(w) is None]
    if missing:
        known = ", ".join(p.slug for p in document.parts)
        print(f"parse: no such part(s): {', '.join(missing)}", file=sys.stderr)
        print(f"parse: derived parts are: {known}", file=sys.stderr)
        return 1

    # A part stage 0 quarantined is not parsed. There is no override: the whole
    # point of the fit checks is that a confidently wrong hierarchy is worse
    # than no hierarchy, so a refusal here is the designed outcome, not a
    # failure to handle.
    fit = profile_data.get("fit_by_part", {})
    refused = [
        (w, fit[w]["alarms"][0]["detail"] if fit[w]["alarms"] else "quarantined")
        for w in wanted
        if w in fit and not fit[w]["passed"]
    ]
    wanted = [w for w in wanted if w not in {r[0] for r in refused}]
    for part_id, reason in refused:
        print(f"parse: REFUSED {part_id}: {reason}", file=sys.stderr)
    if not wanted:
        print("parse: every requested part is quarantined, nothing written", file=sys.stderr)
        return 2

    out_dir = config.OUTPUT / args.run / "layout"
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for part_id in wanted:
        part = document.part_by_id(part_id)
        assert part is not None
        layout = build_layout(config.PDF, document, part, rulebook, config.DOCUMENT_ID, config.BATCHES)
        path = out_dir / f"{part_id}.json"
        layout.write(path)
        numbered = sum(1 for b in layout.blocks if b.block_kind == "numbered")
        tables = sum(1 for b in layout.blocks if b.block_kind == "table")
        prose = sum(1 for b in layout.blocks if b.block_kind == "prose")
        anomalies = len(layout.anomalies) + len(layout.part.anomalies) + sum(
            len(b.anomalies) + sum(len(c.anomalies) for c in b.cells) for b in layout.blocks
        )
        index.append(
            {
                "part": part_id,
                "title": layout.part.title,
                "family": layout.part.family,
                "pages": [layout.part.page_start, layout.part.page_end],
                "template_version": layout.part.template_version,
                "blocks": len(layout.blocks),
                "numbered": numbered,
                "prose": prose,
                "tables": tables,
                "anomalies": anomalies,
            }
        )
        print(
            f"parse: {part_id:<38} pp{layout.part.page_start}-{layout.part.page_end:<4} "
            f"blocks={len(layout.blocks):<5} numbered={numbered:<5} tables={tables:<3} "
            f"anomalies={anomalies:<4} -> {path.relative_to(config.ROOT)}"
        )

    index_path = config.OUTPUT / args.run / "layout" / "_index.json"
    index_path.write_text(
        dump_json(
            {
                "document": config.DOCUMENT_ID,
                "profile": rulebook.name,
                "source_sha256": document.sha256,
                "derived_part_count": len(document.parts),
                "parts_written": index,
                "parts_refused_as_quarantined": [
                    {"part": part_id, "reason": reason} for part_id, reason in refused
                ],
            }
        ),
        encoding="utf-8",
    )
    print(
        f"parse: {len(wanted)} part(s) written, {len(refused)} refused as quarantined, "
        f"index at {index_path.relative_to(config.ROOT)}"
    )
    return 2 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
