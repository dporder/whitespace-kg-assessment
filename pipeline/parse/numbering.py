"""Numbering grammar. The rulebook decides, the parser stays generic.

Every pattern comes from `config.HIERARCHY_PROFILES[<profile>]["numbering"]`
and the ordered level names from `["levels"]`. A rulebook may declare several
ways of printing one level, keyed `<level>_<variant>`, so "item_dotted" and
"heading_bare" are an item and a heading. Nothing about RM6116's numbering is
compiled in here, so a new document family is a config entry and a fixture
test rather than an edit to this file.

One recovery mechanism sits behind the grammar, and it is deliberately not a
second grammar. When a line matches no pattern but is typographically identical
to the headings the part has already produced and its leading integer continues
that part's heading sequence, it is recovered as a heading and the deviation is
recorded on the node. That is what saves Framework Schedule 5's second heading,
which prints "2   Reporting period" with no period after the number while its
seven siblings print "1." through "8." normally: the rulebook's heading pattern
finds seven of eight, and the eighth is recorded rather than lost. The mechanism
is a property of the part's own observed style, not of this document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

# The rulebook's patterns allow leading indentation and none now requires it,
# but a PDF text layer expresses indentation as geometry rather than as
# characters, so every line arrives flush-left. The grammar is applied to the
# line prefixed with one space standing for the indent the layout holds
# geometrically: one space stays inside every `\s{0,N}` bound and satisfies a
# `\s+` anchor, so a rulebook is free to require indentation the way this one
# did before its item pattern was corrected.
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
    variant: str = ""    # the rulebook numbering key that matched
    recovered: bool = False
    anomaly: Optional[str] = None


def _with_variant(match: "NumberMatch", key: str) -> "NumberMatch":
    return replace(match, variant=key)


class Rulebook:
    """The compiled view of one hierarchy profile."""

    def __init__(self, name: str, profile: dict):
        self.name = name
        self.profile = profile
        self.levels: list[str] = list(profile["levels"])
        # levels[0] is "part", which the numbering grammar never produces.
        self.numbered_levels: list[str] = self.levels[1:]
        self.patterns: dict[str, re.Pattern] = {
            key: re.compile(pattern) for key, pattern in profile["numbering"].items()
        }
        # A rulebook may declare several ways of printing one level. The key
        # names the variant, the part before the first underscore names the
        # level it belongs to, so "item_dotted" is an item and "heading_bare"
        # is a heading. A four-level dotted number is the deepest unit the
        # document addresses, which is what an item is, whatever it is printed
        # with; kind follows function, not punctuation.
        self.level_of_key: dict[str, str] = {}
        for key in sorted(self.patterns):
            level = key if key in self.numbered_levels else key.split("_", 1)[0]
            if level not in self.numbered_levels:
                raise ValueError(
                    f"rulebook {name!r} numbering key {key!r} names no level in {self.numbered_levels}"
                )
            self.level_of_key[key] = level
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
        best_rank: tuple = ()
        # Deepest level wins; between two keys of the same depth the level's
        # own name beats a variant, then alphabetical, so the choice is total
        # and deterministic rather than dict-order dependent.
        for key in sorted(self.patterns):
            m = self.patterns[key].match(probe)
            if not m or not m.groups():
                continue
            level = self.level_of_key[key]
            candidate = _build(m.group(1), level, self.depth_of(level), probe, m)
            rank = (candidate.depth, 0 if key == level else -1, key)
            if best is None or rank > best_rank:
                best, best_rank = candidate, rank
                best = _with_variant(candidate, key)
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
