"""Legislation citations: three shapes, all of which must parse (SPEC 2.2).

    1. Title plus year.       `European Union (Withdrawal) Act 2018`
                              `Transfer of Undertakings (Protection of
                               Employment) Regulations 2006`
       Parenthesised qualifiers belong to the title. A greedy regex that stops
       at the first bracket truncates both of these, which is why the title is
       walked backwards token by token instead of matched forwards.

    2. A provision pointer.   `Sections 55 and 56 of the Patents Act 1977`
       One ref per section, like a clause list, with the section in `provision`.

    3. EU instruments.        `Regulation (EU) 2016/679`
       Its own pattern. Folding it into the Act grammar would either miss it or
       corrupt the Act grammar, so it stays separate.

Normalisation mints the key and nothing else. The pointing words on every
citing ref stay exactly as the document wrote them (CLAUDE.md, fidelity).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from pipeline.schemas import Legislation

from .grammar import Citation, Member, _overlaps

YEAR = r"(?:1[6-9]\d{2}|20\d{2})"
INSTRUMENT = r"(?:Act|Regulations|Order|Rules)"

# Words that may sit inside a statute title without ending it. Anything else
# lowercase ends the walk backwards.
TITLE_CONNECTORS = {"of", "and", "the", "for", "to", "in", "on", "no", "de"}

# 3. EU instruments, first: `Regulation (EU) 2016/679`, `Directive (EU) 2019/1`
EU = re.compile(
    r"\b(?P<kind>Regulation|Directive|Decision)\s*\((?P<body>EU|EC|EEC|EURATOM)\)\s*"
    r"(?:No\.?\s*)?(?P<number>\d{1,4}/\d{1,5})",
    re.I,
)
# 2. A provision pointer into a statute.
SECTIONS = re.compile(
    r"\b(?P<unit>[Ss]ections?|[Ss]s?\.|[Aa]rticles?|[Rr]egulations?|[Pp]aragraphs?)\s+"
    r"(?P<list>\d{1,4}[A-Z]?(?:\([0-9a-z]{1,3}\))?"
    r"(?:\s*(?:,|and|to|&|or)\s*\d{1,4}[A-Z]?(?:\([0-9a-z]{1,3}\))?)*)\s+of\s+"
)
# 1. The instrument word plus its year is the anchor; the title is walked back.
INSTRUMENT_YEAR = re.compile(rf"\b(?P<instrument>{INSTRUMENT})\s+(?P<year>{YEAR})\b")

_TOKEN = re.compile(r"\([^()]{1,60}\)|[A-Za-z][\w'’&./-]*|\d+|[^\sA-Za-z\d]")
_SECTION_TOKEN = re.compile(r"\d{1,4}[A-Z]?(?:\([0-9a-z]{1,3}\))?")

INSTRUMENT_KINDS = {"act": "act", "regulations": "regulations", "order": "regulations",
                    "rules": "regulations"}


def slug(text: str) -> str:
    """Key material only. Never applied to anything stored as text."""
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", out)


def key_for(title: str, year: Optional[int], provision: Optional[str] = None,
            instrument_kind: Optional[str] = None) -> str:
    """`legislation/bribery-act-2010`, `legislation/patents-act-1977/section/55`.

    An EU instrument carries its year inside its own number
    (`Regulation (EU) 2016/679`), so appending the year again would mint
    `regulation-eu-2016-679-2016`.
    """
    carries_year = instrument_kind == "eu_regulation"
    stem = slug(f"{title} {year}" if year and not carries_year else title)
    return f"legislation/{stem}" + (f"/{provision}" if provision else "")


def eu_year(number: str) -> Optional[int]:
    """`2016/679` is 2016; `1215/2012` is 2012. EU numbering swapped order in
    2015, so the year is whichever component actually looks like one."""
    parts = number.split("/")
    for part in parts:
        if len(part) == 4 and part.isdigit() and 1950 <= int(part) <= 2099:
            return int(part)
    return None


@dataclass
class LegislationHit:
    span: tuple[int, int]
    surface: str
    title: str                       # qualifiers included, year excluded
    year: Optional[int]
    instrument_kind: str
    provisions: list[tuple[str, tuple[int, int]]]   # (provision, span of its number)
    provision_unit: Optional[str] = None

    def record(self, provision: Optional[str] = None) -> Legislation:
        return Legislation(key=key_for(self.title, self.year, provision,
                                       self.instrument_kind),
                           title=self.title, year=self.year or 0,
                           instrument_kind=self.instrument_kind, provision=provision)


def _walk_back_title(text: str, end: int) -> tuple[str, int]:
    """The statute title ending at `end`, walked backwards over its own tokens.

    A parenthesised qualifier is one token, so `(Withdrawal)` and `(Protection
    of Employment)` are carried into the title instead of truncating it. The
    title returned is the source substring, so its span is exact and nothing
    has to be found again afterwards.
    """
    window_start = max(0, end - 240)
    toks = [(m.group(0), m.start(), m.end())
            for m in _TOKEN.finditer(text, window_start, end)]
    picked: list[tuple[str, int, int]] = []
    i = len(toks) - 1
    while i >= 0:
        token, start, stop = toks[i]
        # Only whitespace may sit between this token and what we already took,
        # otherwise the title is not contiguous and the walk is over.
        right_edge = picked[-1][1] if picked else end
        if text[stop:right_edge].strip():
            break
        if token.startswith("(") or token[:1].isupper():
            picked.append((token, start, stop))
            i -= 1
            continue
        if token.lower() in TITLE_CONNECTORS:
            # A connector belongs to the title only when something capitalised
            # sits immediately to its left, otherwise it is the sentence's word.
            left = toks[i - 1] if i else None
            if (left and not text[left[2]:start].strip()
                    and (left[0][:1].isupper() or left[0].startswith("("))):
                picked.append((token, start, stop))
                i -= 1
                continue
        break
    while picked and picked[-1][0].lower() in TITLE_CONNECTORS:
        picked.pop()                     # "the Bribery Act" is the Bribery Act
    if not picked:
        return "", end
    start = picked[-1][1]
    return text[start:end].strip(), start


def find_legislation(text: str, consumed: Optional[list[tuple[int, int]]] = None
                     ) -> list[LegislationHit]:
    """Every statute citation, longest shapes first so nothing is double-read."""
    taken = list(consumed or [])
    hits: list[LegislationHit] = []

    for m in EU.finditer(text):
        span = (m.start(), m.end())
        if _overlaps(span, taken):
            continue
        title = f"{m.group('kind').title()} ({m.group('body').upper()}) {m.group('number')}"
        hits.append(LegislationHit(span=span, surface=m.group(0), title=title,
                                   year=eu_year(m.group("number")),
                                   instrument_kind="eu_regulation", provisions=[]))
        taken.append(span)

    for m in INSTRUMENT_YEAR.finditer(text):
        if _overlaps((m.start(), m.end()), taken):
            continue
        title_tail, start = _walk_back_title(text, m.start())
        if not title_tail:
            continue
        title = f"{title_tail} {m.group('instrument')}".strip()
        span = (start, m.end())
        kind = INSTRUMENT_KINDS[m.group("instrument").lower()]
        hit = LegislationHit(span=span, surface=text[span[0]:span[1]], title=title,
                             year=int(m.group("year")), instrument_kind=kind,
                             provisions=[])
        # Shape 2: a provision pointer immediately in front of this title.
        pointer = _pointer_before(text, span[0])
        if pointer is not None:
            unit, provisions, pointer_start = pointer
            if not _overlaps((pointer_start, span[0]), taken):
                hit.span = (pointer_start, span[1])
                hit.surface = text[pointer_start:span[1]]
                hit.provisions = provisions
                hit.provision_unit = unit
        hits.append(hit)
        taken.append(hit.span)

    return sorted(hits, key=lambda h: h.span)


def _pointer_before(text: str, title_start: int
                    ) -> Optional[tuple[str, list[tuple[str, tuple[int, int]]], int]]:
    """`Sections 55 and 56 of ` sitting immediately before a statute title."""
    window_start = max(0, title_start - 120)
    for m in SECTIONS.finditer(text, window_start, title_start):
        gap = text[m.end():title_start]
        if gap.strip() not in ("", "the", "The"):
            continue
        unit = m.group("unit").rstrip(".").lower()
        unit = "section" if unit.startswith("s") else unit.rstrip("s")
        provisions = [(t.group(0), (m.start("list") + t.start(), m.start("list") + t.end()))
                      for t in _SECTION_TOKEN.finditer(m.group("list"))]
        if provisions:
            return unit, provisions, m.start()
    return None


def as_citation(hit: LegislationHit) -> Citation:
    """A legislation hit in the shape the rest of detection speaks.

    A pointer into several sections becomes one member per section, anchored to
    its own number's characters, exactly like a clause list.
    """
    members: list[Member] = []
    if hit.provisions:
        for number, span in hit.provisions:
            members.append(Member(number=number, span=span))
    else:
        members.append(Member(number=hit.title, span=hit.span))
    return Citation(span=hit.span, surface=hit.surface, unit=hit.instrument_kind,
                    ref_kind="legislation", members=members, method="grammar",
                    legislation={"title": hit.title, "year": hit.year,
                                 "instrument_kind": hit.instrument_kind,
                                 "provision_unit": hit.provision_unit,
                                 "key": key_for(hit.title, hit.year, None,
                                                hit.instrument_kind)})


def provision_key(hit_meta: dict, number: str) -> str:
    unit = hit_meta.get("provision_unit") or "section"
    return key_for(hit_meta["title"], hit_meta.get("year"), f"{unit}/{number}",
                   hit_meta.get("instrument_kind"))
