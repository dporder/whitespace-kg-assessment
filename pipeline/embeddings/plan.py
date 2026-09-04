"""Deciding what gets embedded at which altitude. Deterministic, no model.

SPEC 2.4 and DESIGN stage 6. Retrieval needs vectors at more than one altitude,
because "who owns the intellectual property" should land on clause 9 while a
query quoting a phrase should land on the leaf that contains it.

    leaf                      -> leaf_text
    leaf, with the flag on    -> leaf_window, REPLACING leaf_text, never both
    container within budget   -> subtree_text
    container over budget     -> summary
    document, part            -> summary, always

The budget is `config.SUBTREE_EMBED_TOKEN_BUDGET`, counted by
`pipeline/embeddings/tokens.py`. A container over it gets a summary because an
embedding averaged over a very long clause drifts toward nothing in particular,
which is the altitude argument, not a cost one.

`leaf_window` is the A/B variant from EVALUATION.md: the leaf embedded with its
previous and next sibling for context. It is off by default, it replaces the
plain leaf embedding rather than doubling storage, and the plan records which
variant produced each item so a retrieval comparison can tell them apart.

This module is a pure function of the trees and config. It runs tonight in full,
with no key and no model; only the summaries and the vectors are blocked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import config
from pipeline.schemas import EmbeddingRecord, Node
from pipeline.embeddings.tokens import ESTIMATOR, estimate_tokens
from pipeline.vocabulary import treeio

LEVELS = ("leaf_text", "leaf_window", "subtree_text", "summary")
ALWAYS_SUMMARY_KINDS = ("document", "part")


@dataclass
class PlanItem:
    """One planned embedding. `text` is empty exactly when a summary is owed."""
    node_id: str
    path: str
    part: str
    kind: str
    level: str
    text: str = ""
    tokens: int = 0
    needs_summary: bool = False
    summary_source: str = ""            # the text a summary would be written from
    reason: str = ""

    def as_dict(self) -> dict:
        out = {"node_id": self.node_id, "path": self.path, "part": self.part,
               "kind": self.kind, "level": self.level, "tokens": self.tokens,
               "needs_summary": self.needs_summary, "reason": self.reason}
        if not self.needs_summary:
            out["text"] = self.text
        else:
            out["summary_source_chars"] = len(self.summary_source)
        return out

    def record(self, vector_ref: str) -> EmbeddingRecord:
        return EmbeddingRecord(node_id=self.node_id, level=self.level, text=self.text,
                               vector_ref=vector_ref,
                               llm_derived=self.level == "summary")


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def by_level(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.level] = out.get(item.level, 0) + 1
        return dict(sorted(out.items()))


def own_text(node: Node) -> str:
    """A leaf's own words. A heading with a title and no body is its title."""
    if node.text:
        return node.text
    return node.title or ""


def window_text(parent: Optional[Node], node: Node) -> str:
    """The leaf with its previous and next anatomy siblings as context."""
    if parent is None:
        return own_text(node)
    siblings = treeio.anatomy_children(parent)
    try:
        i = siblings.index(node)
    except ValueError:
        return own_text(node)
    pieces = []
    if i > 0:
        pieces.append(treeio.subtree_text(siblings[i - 1]))
    pieces.append(own_text(node))
    if i + 1 < len(siblings):
        pieces.append(treeio.subtree_text(siblings[i + 1]))
    return treeio.JOIN.join(p for p in pieces if p)


def build(trees: treeio.Trees, budget: int = None,
          leaf_window: bool = None) -> Plan:
    budget = config.SUBTREE_EMBED_TOKEN_BUDGET if budget is None else budget
    leaf_window = (config.LEAF_WINDOW_EMBEDDING if leaf_window is None
                   else leaf_window)
    plan = Plan()
    for pid, part in trees.ordered():
        parents: dict[str, Node] = {}
        for node in treeio.walk(part):
            for child in treeio.anatomy_children(node):
                parents[child.id] = node
        for node in treeio.walk(part):
            if node.kind == "ref":
                continue
            if node.kind in ALWAYS_SUMMARY_KINDS:
                source = treeio.subtree_text(node)
                plan.items.append(PlanItem(
                    node_id=node.id, path=node.path, part=pid, kind=node.kind,
                    level="summary", needs_summary=True, summary_source=source,
                    tokens=estimate_tokens(source),
                    reason="whole documents and parts always get a summary"))
                continue
            if treeio.is_leaf(node):
                text = (window_text(parents.get(node.id), node) if leaf_window
                        else own_text(node))
                if not text.strip():
                    plan.skipped.append({"node_id": node.id, "path": node.path,
                                         "kind": node.kind,
                                         "reason": "leaf carries no text to embed"})
                    continue
                level = "leaf_window" if leaf_window else "leaf_text"
                plan.items.append(PlanItem(
                    node_id=node.id, path=node.path, part=pid, kind=node.kind,
                    level=level, text=text, tokens=estimate_tokens(text),
                    reason=("leaf embedded with its previous and next sibling; "
                            "replaces leaf_text, never stored beside it"
                            if leaf_window else "leaf embedded on its own words")))
                continue
            source = treeio.subtree_text(node)
            tokens = estimate_tokens(source)
            if not source.strip():
                plan.skipped.append({"node_id": node.id, "path": node.path,
                                     "kind": node.kind,
                                     "reason": "container subtree carries no text"})
                continue
            if tokens <= budget:
                plan.items.append(PlanItem(
                    node_id=node.id, path=node.path, part=pid, kind=node.kind,
                    level="subtree_text", text=source, tokens=tokens,
                    reason=f"subtree fits the {budget}-token budget "
                           f"({tokens} by {ESTIMATOR})"))
            else:
                plan.items.append(PlanItem(
                    node_id=node.id, path=node.path, part=pid, kind=node.kind,
                    level="summary", needs_summary=True, summary_source=source,
                    tokens=tokens,
                    reason=f"subtree is {tokens} tokens by {ESTIMATOR}, over the "
                           f"{budget}-token budget, so it gets a summary"))
    return plan
