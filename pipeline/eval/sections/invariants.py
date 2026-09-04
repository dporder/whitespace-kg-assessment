"""`invariants`: structural and geometric checks, pass or fail with locations.

SPEC 2.1 names the geometric invariants, EVALUATION.md layer 1 names the
structural ones. Every check here is free, deterministic, and runs on every
load. Nothing is repaired: a violation is reported at its path, and it is
either **explained** by an anomaly the parser already recorded on the node, or
counted **unexplained**, which is the zero-tolerance gate
`structural_violations_unexplained_max`.

The anomaly convention. Stage 2 records anomalies as `"<code>[_detail]: prose"`,
as in `numbering_gap_after_9.2: 9.4 follows in source order`. A violation is
explained when an anomaly whose key starts with the check id sits on one of the
two nodes the check actually compared, has not already explained another
violation, and does not contradict what was observed. `AnomalyLedger` holds
those three rules. They matter: without them one recorded gap amnesties every
later gap in the same sibling group, which turns the gate into decoration.

Boxes are compared with GEOMETRY_EPS points of slack, because PDF coordinates
carry rounding noise and a half-point is far below the smallest real indent in
this document family.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from pipeline.eval.context import (BOX_ROUNDTRIP_AGREE, Context, GEOMETRY_EPS,
                                   LIST_CAP)
from pipeline.eval.inputs import walk
from pipeline.eval.rates import MEASURED, NO_DATA, PARTIAL, Rate, Section, cap
from pipeline.schemas import Node, content_hash

CHECKS: dict[str, str] = {
    # geometric, SPEC 2.1
    "child_left_edge": "a child's left edge is at or right of its parent's",
    "own_box_above_first_child": "a node's own box sits at or above its first child's",
    "siblings_ascend": "siblings ascend in reading order without vertical overlap",
    "extent_nests": "a node's extent stays inside its parent's extent",
    "extent_covers_own": "a node's extent covers its own box",
    # structural, EVALUATION.md layer 1
    "branch_or_leaf": "a node has anatomy children or text, never both",
    "path_parent": "a child's path extends its parent's path",
    "label_nesting": "a dotted label extends its parent's dotted label",
    "numbering_gap": "sibling numbering ascends by one",
    "order_preorder": "order is a unique ascending preorder position within the part",
    "content_hash": "a text-bearing node's content_hash matches its text",
    "citable_flags": "intro and ref nodes are not citable",
    "page_range": "page_start <= page_end and a child's pages sit inside its parent's",
    "ref_span_integrity": "a ref's char_span reproduces its pointing words from its parent's text",
}

_DOTTED = re.compile(r"^\d+(?:\.\d+)*$")
_LETTER = re.compile(r"^\(?([a-z]{1,2})\)?$")
_ROMAN = re.compile(r"^\(?((?:x{0,3})(?:ix|iv|v?i{1,3}|v|x))\)?$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10}


class Violation:
    __slots__ = ("check", "part", "path", "detail", "explained_by")

    def __init__(self, check: str, part: str, path: str, detail: str,
                 explained_by: Optional[str] = None):
        self.check, self.part, self.path = check, part, path
        self.detail, self.explained_by = detail, explained_by

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "part": self.part, "path": self.path,
                "detail": self.detail, "explained_by": self.explained_by}


def anomaly_key(anomaly: str) -> str:
    return anomaly.split(":", 1)[0].strip()


_LABEL_TOKEN = re.compile(r"\b\d+(?:\.\d+)*\b|\(([a-z]{1,2}|[ivx]{1,4})\)")


def labels_named_in(anomaly: str) -> set[str]:
    """Numbering labels an anomaly's prose mentions, e.g. {"9.2", "9.4"}."""
    return {m.group(0).strip("()") for m in _LABEL_TOKEN.finditer(anomaly)}


def anomaly_contradicts(check: str, anomaly: str, follower: Optional[str]) -> bool:
    """True when the anomaly's own prose rules out the violation being explained.

    A numbering-gap anomaly that names the labels involved is making a claim
    about *which* gap it describes. "9.4 follows in source order" does not
    explain an observed 9.2 to 9.5 jump; it contradicts it. Without this,
    recording one gap amnesties every later gap in the same sibling group,
    which is precisely the confident wrongness the gate exists to catch.

    An anomaly naming no labels at all is a generic explanation and is allowed.
    """
    if check != "numbering_gap" or follower is None:
        return False
    named = labels_named_in(anomaly)
    if not named:
        return False
    return follower.strip("()") not in named


class AnomalyLedger:
    """Matches violations to the anomalies that explain them, at most one each.

    Three rules, all of them the difference between "recorded and understood"
    and "silently amnestied":

    1. **Location.** Only the two nodes forming the violating pair can explain
       it: the violating node and the node it was compared against. A parent's
       anomaly never explains what happened between its children.
    2. **Consumption.** One anomaly instance explains at most one violation,
       claimed greedily in document order. A second gap in the same group needs
       its own recorded anomaly.
    3. **Consistency.** An anomaly whose prose contradicts the observation does
       not explain it (see `anomaly_contradicts`).
    """

    def __init__(self) -> None:
        self._claimed: set[tuple[str, int]] = set()

    def claim(self, check: str, pair: Iterable[Optional[Node]],
              follower: Optional[str] = None) -> Optional[str]:
        for node in pair:
            if node is None:
                continue
            for i, anomaly in enumerate(node.anomalies):
                if (node.path, i) in self._claimed:
                    continue
                if not anomaly_key(anomaly).startswith(check):
                    continue
                if anomaly_contradicts(check, anomaly, follower):
                    continue
                self._claimed.add((node.path, i))
                return anomaly
        return None

    @property
    def claimed(self) -> set[tuple[str, int]]:
        return set(self._claimed)


def boxes_by_page(node: Node, prefer_own: bool = True) -> dict[int, tuple]:
    """Page -> box. Own ink where the node has any, else its extent.

    A form_row or a table has no ink of its own, only its cells' - falling back
    to the extent is what lets those nodes take part in the geometry checks
    instead of being silently skipped.
    """
    src = node.bboxes_own if (prefer_own and node.bboxes_own) else node.bboxes_extent
    return {b.page: tuple(b.bbox) for b in src}


def extent_by_page(node: Node) -> dict[int, tuple]:
    return {b.page: tuple(b.bbox) for b in node.bboxes_extent}


def roman_to_int(text: str) -> Optional[int]:
    total, prev = 0, 0
    for ch in reversed(text):
        v = _ROMAN_VALUES.get(ch)
        if v is None:
            return None
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total or None


def letters_to_int(text: str) -> int:
    n = 0
    for ch in text:
        n = n * 26 + (ord(ch) - 96)
    return n


def label_sequence_value(label: str, mode: str) -> Optional[int]:
    """The ordinal of a sibling label under the group's numbering mode."""
    raw = label.strip()
    if mode == "dotted":
        if not _DOTTED.match(raw):
            return None
        return int(raw.split(".")[-1])
    if mode == "roman":
        m = _ROMAN.match(raw)
        return roman_to_int(m.group(1)) if m else None
    if mode == "letter":
        m = _LETTER.match(raw)
        return letters_to_int(m.group(1)) if m else None
    return None


def numbering_mode(labels: list[str]) -> Optional[str]:
    """Decide a sibling group's numbering system from its first label.

    "(i)" is both the ninth letter and roman one, so the group's own opening
    label settles it rather than a per-label guess.
    """
    first = labels[0].strip()
    if _DOTTED.match(first):
        return "dotted"
    m = _LETTER.match(first)
    if m and m.group(1) == "i":
        return "roman"
    if m:
        return "letter"
    if _ROMAN.match(first):
        return "roman"
    return None


def _inside(inner: tuple, outer: tuple, eps: float = GEOMETRY_EPS) -> bool:
    return (inner[0] >= outer[0] - eps and inner[1] >= outer[1] - eps
            and inner[2] <= outer[2] + eps and inner[3] <= outer[3] + eps)


def check_tree(part: str, root: Node,
               ledger: Optional[AnomalyLedger] = None
               ) -> tuple[list[Violation], dict[str, int], dict[str, int]]:
    """Every check over one part tree. Returns violations, per-check counts of
    what was actually examined, and per-check counts of what could not be.

    `add`'s second argument is the violating node and `against` is the only
    other node that may explain it: the node it was compared with. Passing a
    parent here for a sibling comparison would let one anomaly amnesty a whole
    sibling group, so the pairs are exactly the two nodes the check looked at.
    """
    violations: list[Violation] = []
    checked: dict[str, int] = {k: 0 for k in CHECKS}
    skipped: dict[str, int] = {k: 0 for k in CHECKS}
    ledger = ledger if ledger is not None else AnomalyLedger()

    def add(check: str, node: Node, detail: str, against: Optional[Node] = None,
            follower: Optional[str] = None) -> None:
        violations.append(Violation(
            check, part, node.path, detail,
            ledger.claim(check, (node, against), follower)))

    seen_order: list[int] = []
    for node in walk(root):
        seen_order.append(node.order)
        anatomy = [c for c in node.children if c.kind != "ref"]

        # -- branch or leaf ---------------------------------------------------
        checked["branch_or_leaf"] += 1
        if anatomy and node.text is not None:
            add("branch_or_leaf", node, "has both anatomy children and text")

        # -- citable flags ----------------------------------------------------
        checked["citable_flags"] += 1
        if node.kind in ("intro", "ref") and node.citable:
            add("citable_flags", node, f"{node.kind} node is citable")

        # -- content hash -----------------------------------------------------
        if node.text is not None:
            checked["content_hash"] += 1
            if node.content_hash is None:
                add("content_hash", node, "text-bearing node has no content_hash")
            elif node.content_hash != content_hash(node.text):
                add("content_hash", node, "content_hash does not match text")
        elif node.content_hash is not None:
            checked["content_hash"] += 1
            add("content_hash", node, "content_hash on a node with no text")

        # -- page range -------------------------------------------------------
        checked["page_range"] += 1
        if node.page_start > node.page_end:
            add("page_range", node,
                f"page_start {node.page_start} > page_end {node.page_end}")

        own = boxes_by_page(node)
        node_extent = extent_by_page(node)

        # -- extent covers own ------------------------------------------------
        if node.bboxes_own and node.bboxes_extent:
            checked["extent_covers_own"] += 1
            for page, box in boxes_by_page(node, prefer_own=True).items():
                if page not in node_extent:
                    add("extent_covers_own", node, f"own box on page {page} is outside the extent")
                elif not _inside(box, node_extent[page]):
                    add("extent_covers_own", node,
                        f"own box {box} on page {page} escapes extent {node_extent[page]}")
        elif node.bboxes_own or node.bboxes_extent:
            skipped["extent_covers_own"] += 1

        for child in anatomy:
            child_box = boxes_by_page(child)
            child_extent = extent_by_page(child)

            # -- path parent --------------------------------------------------
            # Prefix nesting only. How many segments a child adds is a per-kind
            # convention the spec does not fix: a table cell's path carries its
            # row and column (<table>/<row>/<col>), so a one-segment rule would
            # flag correct trees.
            checked["path_parent"] += 1
            if not child.path.startswith(node.path + "/"):
                add("path_parent", child, f"path does not extend parent {node.path}",
                    node)

            # -- label nesting ------------------------------------------------
            if child.label and node.label and _DOTTED.match(child.label) \
                    and _DOTTED.match(node.label):
                checked["label_nesting"] += 1
                if not child.label.startswith(node.label + "."):
                    add("label_nesting", child,
                        f"label {child.label} does not extend parent label {node.label}",
                        node)

            # -- page range nesting -------------------------------------------
            checked["page_range"] += 1
            if child.page_start < node.page_start or child.page_end > node.page_end:
                add("page_range", child,
                    f"pages {child.page_start}-{child.page_end} escape parent "
                    f"{node.page_start}-{node.page_end}", node)

            # -- child left edge ----------------------------------------------
            shared = sorted(set(own) & set(child_box))
            if shared:
                checked["child_left_edge"] += 1
                page = shared[0]
                if child_box[page][0] < own[page][0] - GEOMETRY_EPS:
                    add("child_left_edge", child,
                        f"left edge {child_box[page][0]:.1f} is left of parent's "
                        f"{own[page][0]:.1f} on page {page}", node)
            else:
                skipped["child_left_edge"] += 1

            # -- extent nests --------------------------------------------------
            if child_extent and node_extent:
                checked["extent_nests"] += 1
                for page, box in child_extent.items():
                    if page not in node_extent:
                        add("extent_nests", child,
                            f"extent touches page {page}, outside the parent's extent", node)
                    elif not _inside(box, node_extent[page]):
                        add("extent_nests", child,
                            f"extent {box} on page {page} escapes parent extent "
                            f"{node_extent[page]}", node)
            else:
                skipped["extent_nests"] += 1

        # -- own box above first child ------------------------------------------
        if anatomy:
            first = anatomy[0]
            first_box = boxes_by_page(first)
            if own and first_box:
                checked["own_box_above_first_child"] += 1
                own_first_page = min(own)
                child_first_page = min(first_box)
                if child_first_page < own_first_page:
                    add("own_box_above_first_child", node,
                        f"first child starts on page {child_first_page}, before the "
                        f"node's own page {own_first_page}", first)
                elif child_first_page == own_first_page and \
                        first_box[child_first_page][1] < own[own_first_page][1] - GEOMETRY_EPS:
                    add("own_box_above_first_child", node,
                        f"own box top {own[own_first_page][1]:.1f} is below first child's "
                        f"{first_box[child_first_page][1]:.1f}", first)
            else:
                skipped["own_box_above_first_child"] += 1

        # -- siblings ascend -----------------------------------------------------
        for prev, nxt in zip(anatomy, anatomy[1:]):
            pb, nb = boxes_by_page(prev), boxes_by_page(nxt)
            if not pb or not nb:
                skipped["siblings_ascend"] += 1
                continue
            checked["siblings_ascend"] += 1
            p_page, n_page = max(pb), min(nb)
            if n_page < p_page:
                add("siblings_ascend", nxt,
                    f"starts on page {n_page}, before sibling {prev.label or prev.path} "
                    f"ends on page {p_page}", prev)
                continue
            if n_page > p_page:
                continue                                   # different pages, order is by page
            p, n = pb[p_page], nb[n_page]
            if n[3] <= p[1] + GEOMETRY_EPS:
                add("siblings_ascend", nxt,
                    f"sits entirely above sibling {prev.label or prev.path} on page {p_page}",
                    prev)
            elif n[1] >= p[3] - GEOMETRY_EPS:
                pass                                       # ascending, no overlap
            elif n[0] >= p[2] - GEOMETRY_EPS:
                pass                                       # side by side on one visual line
            else:
                add("siblings_ascend", nxt,
                    f"overlaps sibling {prev.label or prev.path} vertically on page "
                    f"{p_page}: {p} then {n}", prev)

        # -- numbering gaps -------------------------------------------------------
        labelled = [c for c in anatomy if c.label]
        if len(labelled) >= 2:
            mode = numbering_mode([c.label for c in labelled])
            if mode is None:
                skipped["numbering_gap"] += len(labelled) - 1
            else:
                for prev, nxt in zip(labelled, labelled[1:]):
                    a = label_sequence_value(prev.label, mode)
                    b = label_sequence_value(nxt.label, mode)
                    if a is None or b is None:
                        skipped["numbering_gap"] += 1
                        continue
                    checked["numbering_gap"] += 1
                    if b != a + 1:
                        add("numbering_gap", nxt,
                            f"{prev.label} is followed by {nxt.label}", prev,
                            follower=nxt.label)

    # -- order is a unique ascending preorder sequence ---------------------------
    checked["order_preorder"] = len(seen_order)
    if len(set(seen_order)) != len(seen_order):
        violations.append(Violation("order_preorder", part, root.path,
                                    "order values are not unique within the part",
                                    ledger.claim("order_preorder", (root,))))
    elif seen_order != sorted(seen_order):
        first_bad = next(i for i in range(1, len(seen_order))
                         if seen_order[i] < seen_order[i - 1])
        violations.append(Violation(
            "order_preorder", part, root.path,
            f"order descends in preorder at position {first_bad} "
            f"({seen_order[first_bad - 1]} then {seen_order[first_bad]})",
            ledger.claim("order_preorder", (root,))))
    return violations, checked, skipped


def check_ref_spans(ctx: Context,
                    ledger: Optional[AnomalyLedger] = None
                    ) -> tuple[list[Violation], int, int]:
    """A ref's char_span must reproduce its pointing words from its parent."""
    violations: list[Violation] = []
    by_path = ctx.inputs.nodes_by_path()
    checked = skipped = 0
    ledger = ledger if ledger is not None else AnomalyLedger()
    for part in sorted(ctx.inputs.refs):
        for ref in ctx.inputs.refs[part]:
            parent_path = ref.path.rsplit("/ref@", 1)[0]
            parent = by_path.get(parent_path)
            if parent is None or parent.text is None or ref.char_span is None:
                skipped += 1
                continue
            checked += 1
            s, e = ref.char_span
            if parent.text[s:e] != ref.text:
                violations.append(Violation(
                    "ref_span_integrity", part, ref.path,
                    f"chars [{s}:{e}] of {parent_path} read "
                    f"{parent.text[s:e]!r}, ref text is {ref.text!r}",
                    ledger.claim("ref_span_integrity", (ref, parent))))
    return violations, checked, skipped


def distributions(ctx: Context) -> dict[str, Any]:
    """Per-part shape numbers. Informational, never a gate.

    EVALUATION.md layer 1 wants a part whose numbers sit far outside the corpus
    distribution flagged for eyes. With three parts there is no distribution to
    sit outside of, so the harness prints the numbers and says so rather than
    inventing an outlier test.
    """
    rows = []
    for part, tree in sorted(ctx.inputs.trees.items()):
        nodes = list(walk(tree))
        leaves = [n for n in nodes if n.text is not None]
        pages = max(1, tree.page_end - tree.page_start + 1)
        clauses = [n for n in nodes if n.kind in ("clause", "subclause")]
        refs = ctx.inputs.refs.get(part, [])
        depth = max(n.path.count("/") for n in nodes)
        rows.append({
            "part": part, "pages": pages, "nodes": len(nodes), "leaves": len(leaves),
            "clauses": len(clauses), "refs": len(refs), "max_path_depth": depth,
            "nodes_per_page": round(len(nodes) / pages, 2),
            "refs_per_clause": (round(len(refs) / len(clauses), 2) if clauses else None),
        })
    out: dict[str, Any] = {"per_part": rows}
    if len(rows) < 4:
        out["outlier_test"] = (f"not run: {len(rows)} part(s) is too few to define a "
                               f"corpus distribution to sit outside of")
    else:
        out["outlier_test"] = "median absolute deviation over parts, |z| > 3.5 flagged"
        for metric in ("nodes_per_page", "leaves", "clauses"):
            values = sorted(r[metric] for r in rows if r.get(metric) is not None)
            if not values:
                continue
            mid = values[len(values) // 2]
            devs = sorted(abs(v - mid) for v in values)
            mad = devs[len(devs) // 2] or 1e-9
            for r in rows:
                v = r.get(metric)
                if v is None:
                    continue
                z = 0.6745 * (v - mid) / mad
                if abs(z) > 3.5:
                    r.setdefault("flagged", []).append(f"{metric} z={z:.1f}")
    return out


def box_roundtrip(ctx: Context) -> dict[str, Any]:
    """Does a node's stored text actually come from its stored boxes?

    EVALUATION.md layer 1: "every text bearing node's content round trips from
    its bounding boxes". Only meaningful against real pipeline output: fixture
    geometry is fabricated and fixture pages are fixture-local
    (fixtures/README.md), so running it there would measure the fixtures, not
    the parser.
    """
    if ctx.inputs.source != "output":
        return {"status": "skipped",
                "reason": f"input source is {ctx.inputs.source}; fixture geometry is "
                          f"fabricated and fixture-local, so a round trip against the "
                          f"real PDF would not measure the parser"}
    if ctx.options.get("no_pdf"):
        return {"status": "skipped", "reason": "--no-pdf"}
    try:
        import pymupdf
        from rapidfuzz import fuzz
    except Exception as exc:                              # noqa: BLE001
        return {"status": "skipped", "reason": f"dependency unavailable: {exc}"}
    import config
    if not config.PDF.exists():
        return {"status": "skipped", "reason": "PDF not found at config.PDF"}

    mismatches: list[dict[str, Any]] = []
    checked = agreed = 0
    try:
        with pymupdf.open(config.PDF) as doc:
            for part, node in ctx.inputs.nodes():
                if node.text is None or not node.bboxes_own:
                    continue
                extracted = []
                for bb in node.bboxes_own:
                    if not (1 <= bb.page <= doc.page_count):
                        extracted = None
                        break
                    page = doc.load_page(bb.page - 1)
                    extracted.append(page.get_textbox(pymupdf.Rect(*bb.bbox)))
                if extracted is None:
                    continue
                checked += 1
                got = " ".join(" ".join(t.split()) for t in extracted).strip()
                want = " ".join(node.text.split())
                score = fuzz.ratio(got, want) / 100.0
                if score >= BOX_ROUNDTRIP_AGREE:
                    agreed += 1
                else:
                    mismatches.append({"part": part, "path": node.path,
                                       "similarity": round(score, 3),
                                       "from_boxes": got[:160]})
    except Exception as exc:                              # noqa: BLE001
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    shown, hidden = cap(mismatches, LIST_CAP)
    return {"status": "measured",
            "threshold": BOX_ROUNDTRIP_AGREE,
            "agreement": Rate(agreed, checked).as_dict(),
            "mismatches": shown, "mismatches_not_listed": hidden}


def profile_fit(ctx: Context) -> dict[str, Any]:
    """Stage 0's own verdict on whether the rulebook fits, when it exists."""
    if ctx.inputs.profile is None:
        return {"status": "no_data",
                "reason": "output/profile.json absent; stage 0 has not run"}
    fit = ctx.inputs.profile.get("fit")
    return {"status": "measured", "profile": ctx.inputs.profile.get("profile"),
            "fit": fit}


def build(ctx: Context) -> Section:
    s = Section("invariants")
    if not ctx.inputs.trees:
        s.status = NO_DATA
        s.reason = ("no stage 2 trees loaded; nothing to check. "
                    f"looked in {ctx.inputs.root}/tree/")
        s.metrics["structural_violations_unexplained"] = None
        s.data = {"checks": [], "failed_to_load": [r.as_dict() for r in ctx.inputs.failures()]}
        s.line(f"_{s.reason}_")
        return s

    # One ledger across the whole run: an anomaly explains one violation, and
    # which one is decided in document order, parts in sorted order.
    ledger = AnomalyLedger()
    all_violations: list[Violation] = []
    checked: dict[str, int] = {k: 0 for k in CHECKS}
    skipped: dict[str, int] = {k: 0 for k in CHECKS}
    for part in sorted(ctx.inputs.trees):
        v, c, sk = check_tree(part, ctx.inputs.trees[part], ledger)
        all_violations.extend(v)
        for k in CHECKS:
            checked[k] += c.get(k, 0)
            skipped[k] += sk.get(k, 0)

    ref_v, ref_checked, ref_skipped = check_ref_spans(ctx, ledger)
    all_violations.extend(ref_v)
    checked["ref_span_integrity"] += ref_checked
    skipped["ref_span_integrity"] += ref_skipped

    unexplained = [v for v in all_violations if not v.explained_by]
    explained = [v for v in all_violations if v.explained_by]

    # Anomalies the parser recorded that explained no violation. Not an error:
    # the tree may be an excerpt, or the anomaly may describe something this
    # harness does not check. Taken from the ledger's own claim record rather
    # than re-derived, so the two lists cannot drift apart.
    claimed = ledger.claimed
    unmatched_anomalies = []
    anomaly_count = 0
    for part, node in ctx.inputs.nodes():
        for i, a in enumerate(node.anomalies):
            anomaly_count += 1
            if (node.path, i) not in claimed:
                unmatched_anomalies.append({"part": part, "path": node.path, "anomaly": a})

    check_rows = []
    for check, description in CHECKS.items():
        hits = [v for v in all_violations if v.check == check]
        check_rows.append({
            "check": check, "description": description,
            "examined": checked[check], "not_examined": skipped[check],
            "violations": len(hits),
            "unexplained": len([v for v in hits if not v.explained_by]),
            "pass": not [v for v in hits if not v.explained_by],
        })

    shown_v, hidden_v = cap([v.as_dict() for v in all_violations], LIST_CAP)
    shown_a, hidden_a = cap(unmatched_anomalies, LIST_CAP)
    s.status = MEASURED if not ctx.inputs.failures() else PARTIAL
    if ctx.inputs.failures():
        s.reason = f"{len(ctx.inputs.failures())} input file(s) failed to load"
    s.data = {
        "parts_checked": sorted(ctx.inputs.trees),
        "geometry_slack_points": GEOMETRY_EPS,
        "totals": {
            "nodes": sum(1 for _ in ctx.inputs.nodes()),
            "violations": len(all_violations),
            "explained_by_a_recorded_anomaly": len(explained),
            "unexplained": len(unexplained),
        },
        "checks": check_rows,
        "violations": shown_v,
        "violations_not_listed": hidden_v,
        "anomalies": {
            "recorded_total": anomaly_count,
            "with_no_violation_from_this_harness": shown_a,
            "not_listed": hidden_a,
        },
        "failed_to_load": [r.as_dict() for r in ctx.inputs.failures()],
        "profile_fit": profile_fit(ctx),
        "distributions": distributions(ctx),
        "box_roundtrip": box_roundtrip(ctx),
    }
    s.metrics["structural_violations_unexplained"] = len(unexplained)

    total_nodes = s.data["totals"]["nodes"]
    s.line(f"Checked **{total_nodes}** nodes across "
           f"{len(ctx.inputs.trees)} part(s): {', '.join(sorted(ctx.inputs.trees))}.")
    s.line(f"Violations **{len(all_violations)}**, of which "
           f"**{len(explained)}** explained by a recorded anomaly and "
           f"**{len(unexplained)}** unexplained.")
    s.line()
    s.table(["check", "what it means", "examined", "not examined", "violations",
             "unexplained", "pass"],
            [[r["check"], r["description"], r["examined"], r["not_examined"],
              r["violations"], r["unexplained"], "yes" if r["pass"] else "**NO**"]
             for r in check_rows])
    if all_violations:
        s.line()
        s.line("**Locations**")
        s.table(["check", "path", "detail", "explained by"],
                [[v["check"], v["path"], v["detail"],
                  v["explained_by"] or "**unexplained**"] for v in shown_v])
        if hidden_v:
            s.line(f"_{hidden_v} further violation(s) in report.json._")
    if unmatched_anomalies:
        s.line()
        s.line(f"**{len(unmatched_anomalies)}** recorded anomal(ies) had no matching "
               f"violation from this harness (an excerpt, or something it does not check):")
        s.table(["path", "anomaly"], [[a["path"], a["anomaly"]] for a in shown_a])
    fit = s.data["profile_fit"]
    s.line()
    s.bullet(f"stage 0 profile fit: {fit.get('reason') or fit.get('fit')}")
    rt = s.data["box_roundtrip"]
    if rt["status"] == "measured":
        r = rt["agreement"]
        s.bullet(f"box round trip: {Rate(r['count'], r['of'])} of text-bearing nodes "
                 f"reproduce their text from their own boxes")
    else:
        s.bullet(f"box round trip: {rt['status']}, {rt.get('reason')}")
    d = s.data["distributions"]
    s.line()
    s.line("**Distribution over parts** (for eyes, never a gate)")
    s.table(["part", "pages", "nodes", "leaves", "clauses", "refs", "nodes/page",
             "refs/clause"],
            [[r["part"], r["pages"], r["nodes"], r["leaves"], r["clauses"], r["refs"],
              r["nodes_per_page"], r["refs_per_clause"]] for r in d["per_part"]])
    s.bullet(f"outlier test: {d['outlier_test']}")
    return s
