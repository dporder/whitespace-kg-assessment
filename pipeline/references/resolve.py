"""Step two of stage 3: point the pointing words at something, or abstain.

The rules are the document's own (SPEC 2.2, DESIGN 3). Part-local scope first,
then Joint Schedule 1:

    1.3.8  references to Clauses and Schedules mean those of the Core Terms,
           and references in any Schedule to parts, paragraphs, annexes and
           tables mean those of that Schedule
    1.3.9  Paragraphs means the paragraph of the appropriate Schedule
    1.3.10 series are inclusive (applied in detection, where ranges expand)

Nearness is a walk up the tree to the nearest enclosing part, never character
distance. Proximity survives only as one scoring feature among candidates in
the ambiguous residue, which is the one place a guess is allowed to be ranked.

Two hard rules bound everything here. A ref never mints its target: a citation
to a part the corpus does not hold stays unresolved with the conventional id
kept as a candidate *string*. And a deterministic resolver never asserts a
confidence: per SPEC 2.4 and EVALUATION layer 5, its confidence is the measured
precision of its class, attached later by stage 8, so `confidence` stays None
on everything the grammar and the scope rules settle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pipeline.schemas import Candidate

from .corpus import CORE_PART, Corpus
from .detect import Pointer
from .legislation import key_for, provision_key

# How a candidate's score is built. Deterministic, and only ever used to rank
# what a human or a model then judges.
SCORE_STIPULATED = 0.60      # the rule the document itself states
SCORE_LOCAL = 0.40           # the reading a drafter may have meant instead
SCORE_INGESTED = 0.90        # the target exists in the corpus
SCORE_CONVENTIONAL = 0.50    # the id the naming convention would give it
SCORE_TITLE_MATCH = 0.95
SCORE_FAMILY_HINT = 0.05     # proximity, as a scoring feature only
SCORE_ANCESTOR = 0.40


@dataclass
class Resolution:
    status: str
    scope_rule: str
    resolver: str = "scope"
    target_path: Optional[str] = None
    candidates: list[Candidate] = field(default_factory=list)
    confidence: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def top(self, n: int = 5) -> list[Candidate]:
        return sorted(self.candidates, key=lambda c: (-c.score, c.path))[:n]


def _cand(path: str, score: float, reason: str) -> Candidate:
    return Candidate(path=path, score=round(min(max(score, 0.0), 1.0), 3), reason=reason)


def _label_forms(unit: str, number: str) -> list[str]:
    """Label spellings to try, most specific first for the units that carry
    their word in the label.

    An Annex prints its label as "Annex 1", while a clause prints "1". Trying
    the bare number first for an Annex would match the part's own heading 1,
    which is a different provision entirely.
    """
    unit = (unit or "").strip().rstrip("s").title()
    if unit in ("Annex", "Part", "Table"):
        return [f"{unit} {number}", f"{unit.upper()} {number}", number]
    return [number, f"{unit} {number}"]


def _lookup_any(corpus: Corpus, part: str, unit: str, number: str,
                item: Optional[str]) -> Optional[str]:
    for form in _label_forms(unit, number):
        found = corpus.lookup(part, form, item)
        if found:
            return found
    return None


def _ancestor_candidate(corpus: Corpus, part: str, number: str) -> Optional[Candidate]:
    """The deepest numbered ancestor that does exist, for a number that does not."""
    bits = number.split(".")
    while len(bits) > 1:
        bits = bits[:-1]
        found = corpus.lookup(part, ".".join(bits))
        if found:
            return _cand(found, SCORE_ANCESTOR,
                         f"nearest enclosing provision that exists; {part} has no {number}")
    return None


def _schedule_from_context(corpus: Corpus, pointer: Pointer) -> tuple[Optional[str], list[str]]:
    """The schedule a trailing `of ...` names, if it names one."""
    context = pointer.context or {}
    if not context or "schedule" not in (context.get("unit") or ""):
        return None, []
    if context.get("anaphoric"):
        enclosing = corpus.enclosing_part(pointer.parent_path)
        if enclosing and enclosing.is_schedule:
            return enclosing.id, ["scope from 'of this Schedule', the enclosing part"]
        return None, ["'of this Schedule' cited from a part that is not a Schedule"]
    number = context.get("number")
    if not number:
        return None, []
    part_id = Corpus.schedule_part_id(context.get("family"), number)
    if part_id:
        return part_id, [f"scope named explicitly: {context.get('surface')}"]
    return None, [f"scope names a bare Schedule {number}, which family is not stated"]


# --------------------------------------------------------------------------
# the rules, one per ref kind
# --------------------------------------------------------------------------
def _resolve_legislation(pointer: Pointer) -> Resolution:
    """External by definition: the target is a statute, not a provision of this
    corpus, so it is normalised to a key and never looked up in the tree."""
    meta = pointer.legislation or {}
    if pointer.provision:
        target = provision_key(meta, pointer.provision)
    else:
        target = meta.get("key") or key_for(meta.get("title", ""), meta.get("year"),
                                            None, meta.get("instrument_kind"))
    return Resolution(status="external", scope_rule="none", resolver="grammar",
                      target_path=target)


def _resolve_anaphora(corpus: Corpus, pointer: Pointer) -> Resolution:
    """Detected by pattern, resolved only by an LLM or a human (SPEC 2.2)."""
    enclosing = corpus.enclosing_part(pointer.parent_path)
    candidates = []
    if enclosing is not None:
        candidates.append(_cand(enclosing.id, SCORE_INGESTED,
                                "the enclosing part, by tree walk; anaphora is never "
                                "resolved by the grammar"))
    return Resolution(status="ambiguous", scope_rule="none", candidates=candidates,
                      notes=[f"anaphoric reference {pointer.text!r}: the grammar may "
                             f"detect it but may not resolve it"])


def _resolve_clause(corpus: Corpus, pointer: Pointer) -> Resolution:
    """JS1 1.3.8: Clauses mean the Core Terms' clauses, wherever they are cited.

    The pack breaks its own rule three times, writing "Clause 1.x" inside a
    Schedule where the local paragraph 1.x also exists. Those resolve to the
    stipulated target and carry status ambiguous with both candidates and the
    reason, per SPEC 4. Never silently, never dropped.
    """
    number, item = pointer.number, pointer.item
    if not number:
        return Resolution(status="unresolved", scope_rule="none",
                          notes=["clause citation with no number"])
    context_part, notes = _schedule_from_context(corpus, pointer)
    target_part = context_part or CORE_PART
    scope = "js1_1.3.8"
    stipulated = _lookup_any(corpus, target_part, "clause", number, item)
    citing = corpus.enclosing_part(pointer.parent_path)
    local = None
    if citing is not None and not citing.is_core and citing.id != target_part:
        local = _lookup_any(corpus, citing.id, "clause", number, item)

    if stipulated and local:
        reason = (f"the pack writes 'Clause {number}' inside {citing.id}, where "
                  f"paragraph {number} also exists; JS1 1.3.8 stipulates the Core "
                  f"Terms, the drafter may have meant the local provision")
        return Resolution(status="ambiguous", scope_rule=scope, target_path=stipulated,
                          candidates=[_cand(stipulated, SCORE_STIPULATED,
                                            "JS1 1.3.8, the stipulated reading"),
                                      _cand(local, SCORE_LOCAL,
                                            "the local provision of the same number")],
                          notes=notes + [f"mislabelled_cross_reference: {reason}"])
    if stipulated:
        return Resolution(status="resolved", scope_rule=scope, target_path=stipulated,
                          notes=notes)
    if target_part not in corpus.parts:
        return Resolution(status="unresolved", scope_rule=scope,
                          candidates=[_cand(target_part, SCORE_CONVENTIONAL,
                                            f"JS1 1.3.8 points at {target_part}, which "
                                            f"this run has not ingested")],
                          notes=notes + [f"target_part_not_ingested: {target_part}"])
    candidates = []
    ancestor = _ancestor_candidate(corpus, target_part, number)
    if ancestor:
        candidates.append(ancestor)
    if local:
        candidates.append(_cand(local, SCORE_LOCAL,
                                "a provision of that number exists in the citing part"))
    return Resolution(status="unresolved", scope_rule=scope, candidates=candidates,
                      notes=notes + [f"no provision numbered {number} in {target_part}"])


def _resolve_schedule(corpus: Corpus, pointer: Pointer) -> Resolution:
    number = pointer.number
    if not number:
        return Resolution(status="unresolved", scope_rule="none",
                          notes=["schedule citation with no number"])
    citing = corpus.enclosing_part(pointer.parent_path)

    # A parenthetical title is the document's own disambiguator between the
    # three schedule families, so the moment one is present it is the rule that
    # applied, whether or not it lands on a part this run holds.
    scope_with_title = "title_paren" if pointer.title_paren else "js1_1.3.8"
    if pointer.title_paren:
        by_title = corpus.parts_matching_title(pointer.title_paren)
        if len(by_title) == 1:
            return Resolution(status="resolved", scope_rule="title_paren",
                              target_path=by_title[0],
                              notes=[f"title parenthetical {pointer.title_paren!r} "
                                     f"names one ingested part"])

    named = Corpus.schedule_part_id(pointer.family, number)
    if named:
        if corpus.exists(named):
            return Resolution(status="resolved", scope_rule=scope_with_title,
                              target_path=named,
                              notes=[f"family named in the citation: {pointer.family}"])
        reason = ("title parenthetical names the family; part not ingested"
                  if pointer.title_paren else "family named in the citation; part not ingested")
        return Resolution(status="unresolved", scope_rule=scope_with_title,
                          candidates=[_cand(named, SCORE_TITLE_MATCH, reason)],
                          notes=[f"target_part_not_ingested: {named}"])

    if pointer.title_paren:
        # No family word, but the title may name a part the register knows about
        # even though this run has not ingested it.
        registered = [p for p in corpus.known_by_title(pointer.title_paren)
                      if p.endswith(f"-{number}") and not corpus.exists(p)]
        if len(registered) == 1:
            return Resolution(status="unresolved", scope_rule="title_paren",
                              candidates=[_cand(registered[0], 0.9,
                                                "title parenthetical matches; "
                                                "part not ingested")],
                              notes=[f"target_part_not_ingested: {registered[0]}"])

    candidates = []
    for candidate in Corpus.schedule_candidates(number):
        exists = corpus.exists(candidate)
        score = SCORE_INGESTED if exists else SCORE_CONVENTIONAL
        reason = ("bare number, no title parenthetical; this part is ingested" if exists
                  else "bare number, no title parenthetical")
        if citing is not None and citing.family and candidate.startswith(citing.family):
            score += SCORE_FAMILY_HINT
            reason += "; same family as the citing part (proximity, a scoring feature only)"
        candidates.append(_cand(candidate, score, reason))
    note = (f"bare 'Schedule {number}': three families use that number and the "
            f"citation names none")
    if pointer.title_paren:
        note = (f"'Schedule {number} ({pointer.title_paren})': the title matches no "
                f"part this run knows, so the family is still open")
    # Nothing settled it, so no scope rule applied: "none" is the honest stamp.
    return Resolution(status="ambiguous",
                      scope_rule="title_paren" if pointer.title_paren else "none",
                      candidates=candidates, notes=[note])


def _resolve_paragraph(corpus: Corpus, pointer: Pointer) -> Resolution:
    """JS1 1.3.9: Paragraph means the paragraph of the appropriate Schedule."""
    number, item = pointer.number, pointer.item
    if not number:
        return Resolution(status="unresolved", scope_rule="none",
                          notes=["paragraph citation with no number"])
    context_part, notes = _schedule_from_context(corpus, pointer)
    citing = corpus.enclosing_part(pointer.parent_path)
    target_part = context_part
    if target_part is None:
        if citing is not None and citing.is_schedule:
            target_part = citing.id
            notes.append("the appropriate Schedule is the enclosing part, by tree walk")
        else:
            candidates = []
            if citing is not None:
                local = _lookup_any(corpus, citing.id, "paragraph", number, item)
                if local:
                    candidates.append(_cand(local, SCORE_LOCAL,
                                            "a provision of that number in the citing part"))
            return Resolution(status="ambiguous", scope_rule="js1_1.3.9",
                              candidates=candidates,
                              notes=notes + ["JS1 1.3.9 points at the appropriate Schedule, "
                                             "and this citation names none while sitting "
                                             "outside a Schedule"])
    if target_part not in corpus.parts:
        return Resolution(status="unresolved", scope_rule="js1_1.3.9",
                          candidates=[_cand(target_part, SCORE_CONVENTIONAL,
                                            "the Schedule this paragraph belongs to has "
                                            "not been ingested")],
                          notes=notes + [f"target_part_not_ingested: {target_part}"])
    found = _lookup_any(corpus, target_part, "paragraph", number, item)
    if found:
        return Resolution(status="resolved", scope_rule="js1_1.3.9", target_path=found,
                          notes=notes)
    candidates = []
    ancestor = _ancestor_candidate(corpus, target_part, number)
    if ancestor:
        candidates.append(ancestor)
    return Resolution(status="unresolved", scope_rule="js1_1.3.9", candidates=candidates,
                      notes=notes + [f"no paragraph numbered {number} in {target_part}"])


def _resolve_inside_schedule(corpus: Corpus, pointer: Pointer) -> Resolution:
    """Parts, annexes and tables inside a Schedule mean that Schedule's (1.3.8)."""
    number = pointer.number
    if not number:
        return Resolution(status="unresolved", scope_rule="none",
                          notes=[f"{pointer.ref_kind} citation with no number"])
    context_part, notes = _schedule_from_context(corpus, pointer)
    citing = corpus.enclosing_part(pointer.parent_path)
    target_part = context_part or (citing.id if citing else None)
    if target_part is None:
        return Resolution(status="unresolved", scope_rule="none",
                          notes=notes + ["no enclosing part to scope to"])
    scope = "js1_1.3.8" if (citing and citing.is_schedule) else "same_part"
    if target_part not in corpus.parts:
        return Resolution(status="unresolved", scope_rule=scope,
                          candidates=[_cand(target_part, SCORE_CONVENTIONAL,
                                            "the part this belongs to is not ingested")],
                          notes=notes + [f"target_part_not_ingested: {target_part}"])
    found = _lookup_any(corpus, target_part, pointer.ref_kind, number, pointer.item)
    if found:
        return Resolution(status="resolved", scope_rule=scope, target_path=found,
                          notes=notes)
    return Resolution(status="unresolved", scope_rule=scope,
                      notes=notes + [f"no {pointer.ref_kind} {number} in {target_part}"])


def resolve_pointer(corpus: Corpus, pointer: Pointer) -> Resolution:
    """The deterministic pass. LLM-free by construction: nothing below imports
    `pipeline.llm`, and the residue call happens afterwards, in residue.py."""
    if pointer.ref_kind == "legislation":
        return _resolve_legislation(pointer)
    if pointer.anaphoric:
        return _resolve_anaphora(corpus, pointer)
    if pointer.ref_kind == "clause":
        return _resolve_clause(corpus, pointer)
    if pointer.ref_kind == "schedule":
        return _resolve_schedule(corpus, pointer)
    if pointer.ref_kind == "paragraph":
        return _resolve_paragraph(corpus, pointer)
    if pointer.ref_kind in ("annex", "part"):
        return _resolve_inside_schedule(corpus, pointer)
    return Resolution(status="unresolved", scope_rule="none",
                      notes=[f"{pointer.unit!r} is not a unit this document numbers; "
                             f"the pointer is kept with kind unknown rather than guessed"])
