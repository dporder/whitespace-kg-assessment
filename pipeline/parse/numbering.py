"""Numbering grammar. The rulebook decides, the parser stays generic.

Every pattern comes from `config.HIERARCHY_PROFILES[<profile>]["numbering"]`
and the ordered level names from `["levels"]`. Nothing about RM6116's numbering
is compiled in here, so a new document family is a config entry and a fixture
test rather than an edit to this file.

One recovery mechanism sits behind the grammar, and it is deliberately not a
second grammar. When a line matches no pattern but is typographically identical
to the headings the part has already produced and its leading integer continues
that part's heading sequence, it is recovered as a heading and the deviation is
recorded on the node. That is what saves Framework Schedule 5's second heading,
which prints "2   Reporting period" with no period after the number while its
six siblings print "1." through "7." normally: the rulebook's heading pattern
finds six of seven, and the seventh is recorded rather than lost. The mechanism
is a property of the part's own observed style, not of this document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# The rulebook's patterns are written against a line that carries its own
# leading indentation as whitespace: three of the four allow it (`\s{0,4}`,
# `\s{0,10}`, `\s{0,12}`) and the item pattern requires it (`^\s+\(`). A PDF
# text layer expresses indentation as geometry, not as characters, so every
# line arrives flush-left and the item pattern matches nothing at all: zero of
# the 169 lettered items in Core Terms, against 169 when the same pattern is
# anchored `^\s*`. Rather than edit the frozen rulebook, the grammar is applied
# to the line prefixed with one space standing for the indent the layout holds
# geometrically. One space satisfies `\s+` and stays inside every `\s{0,N}`
# bound, so the rulebook's intent is preserved exactly and the adaptation
# survives config.py being corrected. See the parser-builder report.
INDENT_SENTINEL = " "

# A line that opens with something number-shaped. Used only to measure how much
# numbering the rulebook fails to cover (quarantine check 2); it never assigns
# a level of its own.
# A bare integer followed by a space is prose far more often than numbering:
# "15 Working Days of the notification", "7 (Call-Off Award Procedure) and must
# state ...". Counting those as numbering the rulebook failed to cover would
# measure the wrapping, not the grammar, so a token has to carry a separator to
# count: a dot between components, or a trailing dot or bracket.
NUMBER_SHAPED = re.compile(
    r"^\s{0,8}("
    r"\d{1,3}(?:\.\d{1,3}){1,4}\.?"          # 3.1 / 3.1.2 / 3.1.2.4 / 1.1.
    r"|\d{1,3}[.)]"                          # 3. / 3)
    r"|\(?[a-zA-Z]{1,3}\)"                   # (a) / (iv) / a)
    r"|[ivxlIVXL]{1,6}[.)]"                  # roman with a dot or bracket
    r")\s"
)


@dataclass(frozen=True)
class NumberMatch:
    label: str          # the printed number, verbatim: "3.1.2", "(a)", "35."
    token: str          # the whitespace-delimited word the label came from
    level: str          # rulebook level name
    depth: int          # 1-based index into the rulebook's levels below "part"
    key: str            # path segment: "3.1.2" or "a"
    dotted_depth: int   # how many dotted components, 0 for lettered/roman items
    rest_start: int = 0  # index into the line text where the node's own words begin
    recovered: bool = False
    anomaly: Optional[str] = None


class Rulebook:
    """The compiled view of one hierarchy profile."""

    def __init__(self, name: str, profile: dict):
        self.name = name
        self.profile = profile
        self.levels: list[str] = list(profile["levels"])
        # levels[0] is "part", which the numbering grammar never produces.
        self.numbered_levels: list[str] = self.levels[1:]
        self.patterns: dict[str, re.Pattern] = {
            level: re.compile(pattern) for level, pattern in profile["numbering"].items()
        }
        self.max_dotted_depth: int = int(profile["max_dotted_depth"])
        self.citable_kinds: list[str] = list(profile.get("citable_kinds", []))
        self.interpretation_cues: list[re.Pattern] = [
            re.compile(cue) for cue in profile.get("interpretation_cues", [])
        ]
        self.unit_labels: dict[str, str] = dict(profile.get("unit_labels", {}))
        self.units_from_document: list[str] = list(profile.get("unit_labels_from_document", []))
        self.units_from_profile: list[str] = list(profile.get("unit_labels_from_profile", []))

    def depth_of(self, level: str) -> int:
        return self.numbered_levels.index(level) + 1

    def match(self, text: str) -> Optional[NumberMatch]:
        """Deepest rulebook level whose pattern matches the start of `text`.

        Deepest wins so that "3.1.2" is a subclause rather than a clause whose
        pattern also matches its prefix.
        """
        probe = INDENT_SENTINEL + text
        best: Optional[NumberMatch] = None
        for level in self.numbered_levels:
            pattern = self.patterns.get(level)
            if pattern is None:
                continue
            m = pattern.match(probe)
            if not m or not m.groups():
                continue
            candidate = _build(m.group(1), level, self.depth_of(level), probe, m)
            if best is None or candidate.depth > best.depth:
                best = candidate
        return best

    def looks_numbered(self, text: str) -> bool:
        return bool(NUMBER_SHAPED.match(INDENT_SENTINEL + text))


def _build(raw: str, level: str, depth: int, probe: str, m: re.Match) -> NumberMatch:
    matched = probe[m.start(): m.end()].strip()
    token = matched.split()[0] if matched else raw
    dotted = raw.count(".") + 1 if re.fullmatch(r"\d{1,3}(?:\.\d{1,3})*", raw) else 0
    key = raw if dotted else raw.strip("()")
    label = raw if dotted else f"({raw.strip('()')})"
    return NumberMatch(
        label=label,
        token=token,
        level=level,
        depth=depth,
        key=key,
        dotted_depth=dotted,
        rest_start=max(0, m.end() - len(INDENT_SENTINEL)),
    )


HEADING_RECOVERY_ANOMALY = "heading_number_missing_period"


def recover_heading(
    text: str,
    rulebook: Rulebook,
    expected_number: Optional[int],
    style_matches: bool,
) -> Optional[NumberMatch]:
    """Recover a heading the rulebook's pattern missed.

    Fires only when all three hold: the line carries the part's own heading
    typography, its leading integer is exactly the next one in the part's
    heading sequence, and the rulebook matched nothing. Anything less is left
    unrecovered and shows up in the orphan rate, which is the honest outcome.
    """
    if not style_matches or expected_number is None:
        return None
    m = re.match(r"^\s{0,8}(\d{1,3})\s+(?=[A-Z])", INDENT_SENTINEL + text)
    if not m:
        return None
    if int(m.group(1)) != expected_number:
        return None
    level = rulebook.numbered_levels[0]
    return NumberMatch(
        label=m.group(1),
        token=m.group(1),
        level=level,
        depth=1,
        key=m.group(1),
        dotted_depth=1,
        rest_start=max(0, m.end() - len(INDENT_SENTINEL)),
        recovered=True,
        anomaly=(
            f"{HEADING_RECOVERY_ANOMALY}: printed {m.group(0).strip()!r}, "
            f"the rulebook heading pattern requires a period after the number; "
            f"recovered from heading typography and sequence position"
        ),
    )
