"""Detection: the citation grammar, the orphan scan, and anaphora by pattern.

SPEC 2.2, detection: "an anchor unit word (Clause, Paragraph, Schedule, Annex,
Part, Section, Act, Regulations) followed by a number and list grammar
(numbers, dots, commas, and, to, ranges, optional parenthetical titles).
Behind the grammar runs an orphan keyword detector, any unit keyword not
covered by a detected span is surfaced for triage as either generic prose use
or a missed citation. The fallback ladder is grammar, then orphan scan, then
LLM span extraction on orphan sentences only, then the review queue. Anaphoric
forms (this Schedule, that Clause) are detected by pattern but resolved only by
LLM or human."

Nothing here resolves anything, and nothing here reads the corpus. This module
answers one question: which characters of this node's text are pointing words,
and what shape of pointer are they.

Two rules from the spec have consequences worth stating at the top.

A list phrase becomes one ref per cited target, each anchored to its own
number's characters. So `Clauses 3.1.1 and 3.1.2` yields two members whose
spans cover `3.1.1` and `3.1.2`, while a lone `Clause 3.1.2` yields one member
whose span covers the whole phrase including the anchor word, which is what the
committed fixtures show.

Ranges expand inclusively (JS1 1.3.10). The endpoints own their characters; the
members between them own none, because the document never wrote them. They are
carried as `expanded` members and the resolver gives them a path suffix so two
of them can never collide on one id. See `detect.py` for that, and the report
in `handover/` for why the path format needed the extension.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

# The anchor set is exactly SPEC 2.2's. `Table` is a unit the interpretation
# clause names (JS1 1.3.8) but the spec's citation grammar does not, so it is
# watched by the orphan detector instead of minting refs.
UNIT_TO_REF_KIND = {
    "clause": "clause", "clauses": "clause",
    "paragraph": "paragraph", "paragraphs": "paragraph",
    "schedule": "schedule", "schedules": "schedule",
    "annex": "annex", "annexes": "annex",
    "part": "part", "parts": "part",
    # A Section with no statute behind it is not a unit this document numbers,
    # so it is a pointer of unknown kind rather than a guessed clause.
    "section": "unknown", "sections": "unknown",
}
ORPHAN_KEYWORDS = ("clause", "clauses", "paragraph", "paragraphs", "schedule",
                   "schedules", "annex", "annexes", "part", "parts", "section",
                   "sections", "act", "regulations", "table", "tables",
                   "sub-paragraph", "sub-paragraphs", "subparagraph")

FAMILY = r"(?:Framework|Joint|Call[-\s]?[Oo]ff)"
UNIT = r"(?:Clauses|Clause|Paragraphs|Paragraph|Schedules|Schedule|Annexes|Annex|Parts|Part|Sections|Section)"
NUM = r"\d{1,3}(?:\.\d{1,3}){0,3}"
ITEM = r"(?:\((?:[a-z]{1,3}|[ivxlcdm]{1,6})\))"
TITLE = r"(?:\s*\([^()]{2,80}\))"
LETTER = r"[A-Z](?![a-zA-Z])"
TOKEN = rf"(?:{NUM}{ITEM}?|{LETTER})"
RANGE_WORDS = (" to ", " through ", "-", "–", "—")
SEP = r"(?:\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*|\s+to\s+|\s+through\s+|\s*[-–—]\s*)"

# The unit and family words are matched case-insensitively because the pack
# writes both ("Clause 9.2" but "paragraph 5 of this Schedule", 27 times).
# The flag is scoped rather than global: a global re.I would make the [A-Z]
# letter token match lowercase words and mint refs out of ordinary prose.
CITATION = re.compile(
    rf"\b(?:(?P<family>(?i:{FAMILY}))\s+)?(?P<unit>(?i:{UNIT}))\s+"
    rf"(?P<list>{TOKEN}{TITLE}?(?:{SEP}{TOKEN}{TITLE}?)*)"
)
_TOKEN_RE = re.compile(rf"(?P<num>{NUM})(?P<item>{ITEM})?|(?P<letter>{LETTER})")
_SEP_RE = re.compile(SEP)
_TITLE_RE = re.compile(r"\s*\((?P<title>[^()]{2,80})\)")

# "of Schedule 6", "of this Schedule", "of the Core Terms"
CONTEXT = re.compile(
    rf"\A\s*(?:of|in|to)\s+(?:the\s+)?(?P<this>this\s+|that\s+)?"
    rf"(?:(?P<family>{FAMILY})\s+)?"
    rf"(?P<unit>Schedules?|Parts?|Annexe?s?|Clauses?|Paragraphs?|Core\s+Terms|Agreement|Contract)"
    rf"(?:\s+(?P<number>{NUM}))?",
    re.I,
)

ANAPHORA = re.compile(
    r"\b(?P<det>this|that|these|those)\s+"
    r"(?P<unit>Schedules?|Clauses?|Paragraphs?|Annexe?s?|Parts?|Sections?)\b",
    re.I,
)

_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+")


@dataclass
class Member:
    """One cited target inside a phrase, with the characters it owns."""
    number: str
    span: tuple[int, int]
    item: Optional[str] = None
    title_paren: Optional[str] = None
    expanded: bool = False          # minted by a range: owns no characters of its own
    expansion_index: Optional[int] = None


@dataclass
class Citation:
    """One detected pointing phrase, before anything has been resolved."""
    span: tuple[int, int]
    surface: str
    unit: str                        # the anchor word as written
    ref_kind: str
    members: list[Member] = field(default_factory=list)
    family: Optional[str] = None
    context: Optional[dict] = None   # the "of Schedule 6" / "of this Schedule" tail
    method: str = "grammar"
    anaphoric: bool = False
    notes: list[str] = field(default_factory=list)
    legislation: Optional[dict] = None   # set by legislation.py


def _split_last(number: str) -> tuple[str, Optional[int]]:
    head, _, tail = number.rpartition(".")
    try:
        return head, int(tail)
    except ValueError:
        return head, None


def expand_range(first: str, last: str, limit: int) -> list[str]:
    """Inclusive expansion of `first` to `last`, interior members only.

    Only expands when both endpoints share a prefix and differ in an integer
    final component. `10.4.3 to 11.2` shares no prefix, so it does not expand:
    a guessed series is worse than a recorded one.
    """
    head_a, tail_a = _split_last(first)
    head_b, tail_b = _split_last(last)
    if head_a != head_b or tail_a is None or tail_b is None:
        return []
    if not 0 < tail_b - tail_a <= limit:
        return []
    prefix = f"{head_a}." if head_a else ""
    return [f"{prefix}{n}" for n in range(tail_a + 1, tail_b)]


def _parse_list(list_text: str, offset: int, max_range: int) -> tuple[list[Member], list[str]]:
    """Tokens, separators, titles and ranges, with absolute character offsets."""
    members: list[Member] = []
    notes: list[str] = []
    pos = 0
    pending_range = False
    while pos < len(list_text):
        m = _TOKEN_RE.match(list_text, pos)
        if not m:
            break
        number = m.group("num") or m.group("letter")
        item = (m.group("item") or "").strip("()") or None
        start, end = m.start(), m.end()
        title = None
        t = _TITLE_RE.match(list_text, end)
        if t:
            title = t.group("title").strip()
            end = t.end()
        if pending_range and members:
            opener = members[-1].number
            interior = expand_range(opener, number, max_range)
            if not interior and opener != number:
                head_a, tail_a = _split_last(opener)
                head_b, tail_b = _split_last(number)
                if head_a != head_b:
                    notes.append(f"range_not_expanded: {opener} to {number} "
                                 f"share no numbering prefix")
                elif tail_a is not None and tail_b is not None and tail_b > tail_a:
                    notes.append(f"range_not_expanded: {opener} to {number} "
                                 f"exceeds the expansion limit of {max_range}")
            for i, num in enumerate(interior, start=1):
                # An interior member owns no characters: the document never
                # wrote its number. `find_citations` sets its span to the whole
                # range phrase, and `expansion_index` is what keeps its id
                # distinct from its siblings'.
                members.append(Member(number=num, span=(0, 0), expanded=True,
                                      expansion_index=i))
            pending_range = False
        members.append(Member(number=number, span=(offset + start, offset + end),
                              item=item, title_paren=title))
        pos = end
        s = _SEP_RE.match(list_text, pos)
        if not s:
            break
        pending_range = any(w in s.group(0) for w in RANGE_WORDS)
        pos = s.end()
    return members, notes


def find_citations(text: str, *, max_range: int = 60,
                   consumed: Optional[list[tuple[int, int]]] = None) -> list[Citation]:
    """Every anchor-word citation in `text`, skipping already-consumed spans."""
    taken = list(consumed or [])
    out: list[Citation] = []
    for m in CITATION.finditer(text):
        span = (m.start(), m.end())
        if _overlaps(span, taken):
            continue
        members, notes = _parse_list(m.group("list"), m.start("list"), max_range)
        if not members:
            continue
        # A range's interior members are anchored to the range phrase, which is
        # the only ink the document gave them.
        for i, member in enumerate(members):
            if member.expanded:
                previous = next((x for x in reversed(members[:i]) if not x.expanded), None)
                following = next((x for x in members[i + 1:] if not x.expanded), None)
                if previous and following:
                    member.span = (previous.span[0], following.span[1])
        unit = m.group("unit")
        kind = UNIT_TO_REF_KIND.get(unit.lower(), "unknown")
        citation = Citation(span=span, surface=m.group(0), unit=unit, ref_kind=kind,
                            members=members, family=m.group("family"), notes=notes)
        tail = CONTEXT.match(text[m.end():])
        if tail:
            citation.context = {
                "unit": (tail.group("unit") or "").lower().replace("  ", " "),
                "family": tail.group("family"),
                "number": tail.group("number"),
                "anaphoric": bool(tail.group("this")),
                "span": (m.end() + tail.start(), m.end() + tail.end()),
                "surface": tail.group(0).strip(),
            }
        taken.append(span)
        if citation.context and citation.context["anaphoric"]:
            # "of this Schedule" is the citation's own scope tail, not a second
            # citation, so it does not also surface as anaphora or as an orphan.
            taken.append(citation.context["span"])
        out.append(citation)
    return sorted(out, key=lambda c: c.span)


def find_anaphora(text: str, consumed: list[tuple[int, int]]) -> list[Citation]:
    """"this Schedule", "that Clause". Detected here, never resolved here."""
    out = []
    for m in ANAPHORA.finditer(text):
        span = (m.start(), m.end())
        if _overlaps(span, consumed):
            continue
        unit = m.group("unit").lower().rstrip("s") or m.group("unit").lower()
        kind = UNIT_TO_REF_KIND.get(unit, UNIT_TO_REF_KIND.get(unit + "s", "unknown"))
        out.append(Citation(span=span, surface=m.group(0), unit=m.group("unit"),
                            ref_kind=kind, members=[], method="anaphora",
                            anaphoric=True,
                            notes=[f"anaphoric_{m.group('det').lower()}"]))
        consumed.append(span)
    return out


def sentences(text: str) -> list[tuple[int, int, str]]:
    """Rough sentence spans, for orphan triage and the LLM's context window.

    Legal prose is full of abbreviations and semicolon-separated limbs, so this
    is deliberately only used to give a human or a model something to read, and
    never to decide anything.
    """
    out, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        out.append((start, m.start(), text[start:m.start()]))
        start = m.end()
    if start < len(text):
        out.append((start, len(text), text[start:]))
    return out or [(0, len(text), text)]


def find_orphans(text: str, consumed: list[tuple[int, int]]) -> list[dict]:
    """Unit keywords no detected span covers, triaged rather than dropped."""
    out = []
    spans = sentences(text)
    for m in re.finditer(r"\b(" + "|".join(sorted(ORPHAN_KEYWORDS, key=len, reverse=True))
                         + r")\b", text, re.I):
        span = (m.start(), m.end())
        if _overlaps(span, consumed):
            continue
        word = m.group(0)
        after = text[m.end():m.end() + 4]
        sentence = next((s for s in spans if s[0] <= m.start() < s[1]), (0, len(text), text))
        capitalised = word[:1].isupper()
        sentence_initial = m.start() == sentence[0]
        if re.match(r"\s*(?:\d|\(|[A-Z]\b)", after):
            verdict = "possible_missed_citation"
        elif capitalised and not sentence_initial:
            verdict = "capitalised_no_number"
        else:
            verdict = "generic_prose"
        out.append({"keyword": word, "char_span": [span[0], span[1]],
                    "verdict": verdict, "sentence_span": [sentence[0], sentence[1]],
                    "sentence": sentence[2]})
    return out


def _overlaps(span: tuple[int, int], taken: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in taken)


def iter_spans(citations: list[Citation]) -> Iterator[tuple[int, int]]:
    for c in citations:
        yield c.span
