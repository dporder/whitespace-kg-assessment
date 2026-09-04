"""The deterministic per-section typo-density signal.

SPEC 2.3: "A deterministic per section typo density signal (spelling and obvious
grammar checks) forces `typo_dense` on matches from high typo sections, because
there a capital letter may be an accident and a missing one may hide a real
use." DESIGN tier 2 makes the reason explicit: in typo-dense text capitalisation
stops being evidence in *both* directions, so those matches go to their own
narrow check rather than being trusted or dropped.

Two constraints shape the implementation. It must be deterministic, so no
system dictionary (`/usr/share/dict/words` is not the same file on two
machines) and no network. And it must not need a dependency the repo does not
already have. So the "spelling" half is built from the corpus itself plus
orthographic rules that need no word list at all:

* a lowercase letter glued to the front of a capitalised word (`rFramework`,
  which this pack really contains and the Award Form really prints),
* an uppercase letter loose inside a word (`SUpplier`), sparing the shapes that
  are legitimately mixed-case,
* three identical letters in a row, and vowel-less alphabetic words that are not
  acronyms,
* **rare-near-common**: a token that occurs once in the corpus and is one edit
  away from a token that occurs often. That is a dictionary-free misspelling
  detector, and the corpus it draws on is the document itself, which is the only
  authority available inside an air-gapped boundary.

The grammar half is deliberately narrow, only doubled words, because the shapes
that look like grammar errors in a PDF text layer (spacing round punctuation)
are usually extraction artefacts and would inflate the signal with noise.

The density is flagged word tokens over all word tokens in the section's derived
subtree text, and the threshold is `config.TYPO_DENSITY_THRESHOLD`. Section here
means a part's top-level child, the same unit the concept scan uses.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from pipeline.vocabulary import treeio
from pipeline.vocabulary.text import word_tokens

# A token must occur at least this often to count as the "correct" spelling a
# once-seen neighbour is probably a typo of.
COMMON_MIN_FREQ = 5
RARE_MAX_FREQ = 1
MIN_EDIT_CANDIDATE_LEN = 5      # below this, one edit is a different word, not a typo

# The rare-near-common detector infers "this token occurs once, so it is
# probably a typo of the common one". That premise needs a corpus big enough for
# ordinary vocabulary to have recurred. Below this many word tokens the detector
# is switched off and says so, rather than flagging `provided` as a typo of
# `provider` because a 26-word fixture contains each once.
MIN_CORPUS_TOKENS_FOR_EDIT_CHECK = 20_000

# Endings that make two tokens inflections of one stem rather than a misspelling
# of each other.
_INFLECTIONS = {"", "s", "d", "e", "y", "r", "n", "ed", "er", "es", "en", "ly",
                "al", "ing", "ies", "ied", "ers"}

_STRAY_PREFIX = re.compile(r"^[a-z][A-Z][a-z]")
_TRIPLE = re.compile(r"(.)\1\1", re.I)
_VOWEL = re.compile(r"[AEIOUYaeiouy]")
# Mixed case that is legitimate rather than a typo. `IPRs` and `SMEs` are
# acronym plurals, not stray capitals, and this pack is full of them.
_LEGIT_MIXED = re.compile(
    r"^(?:[A-Z]+(?:s|'s|’s)?|[A-Z][a-z0-9'’]*|[a-z]+|"
    r"(?:Mc|Mac|O'|O’)[A-Z][a-z]+|"
    r"[A-Z][a-z]*(?:-[A-Z][a-z]*)*|[a-z]+(?:-[A-Z][a-z]*)*)$")


@dataclass
class SectionSignal:
    part: str
    section_path: str
    tokens: int
    flagged: int
    reasons: Counter = field(default_factory=Counter)
    examples: list[dict] = field(default_factory=list)

    @property
    def density(self) -> float:
        return (self.flagged / self.tokens) if self.tokens else 0.0

    def as_dict(self, threshold: float) -> dict:
        return {
            "part": self.part, "section_path": self.section_path,
            "word_tokens": self.tokens, "flagged_tokens": self.flagged,
            "density": round(self.density, 6),
            "typo_dense": self.density >= threshold,
            "reasons": dict(sorted(self.reasons.items())),
            "examples": self.examples[:10],
        }


def corpus_frequencies(trees: treeio.Trees) -> Counter:
    """Case-insensitive token frequencies across every loaded tree."""
    freq: Counter = Counter()
    for _part, node in trees.nodes():
        if node.kind == "ref":
            continue
        for value in (node.text, node.title):
            if value:
                for _off, tok in word_tokens(value):
                    freq[tok.lower()] += 1
    return freq


def _near_common_index(freq: Counter) -> dict[int, list[str]]:
    """Common tokens bucketed by length, for the one-edit neighbour search."""
    buckets: dict[int, list[str]] = {}
    for tok, count in freq.items():
        if count >= COMMON_MIN_FREQ and len(tok) >= MIN_EDIT_CANDIDATE_LEN:
            buckets.setdefault(len(tok), []).append(tok)
    for length in buckets:
        buckets[length].sort()
    return buckets


def _inflection_pair(a: str, b: str) -> bool:
    """Are these two forms of one stem rather than a misspelling of each other?

    `provided` and `provider` differ by one edit and are both ordinary English;
    `calloff` and `callof` differ by one edit and one of them is a typo. The
    difference is whether the two tails are both inflectional endings.
    """
    stem = 0
    for x, y in zip(a, b):
        if x != y:
            break
        stem += 1
    return a[stem:] in _INFLECTIONS and b[stem:] in _INFLECTIONS


def _rare_near_common(token: str, freq: Counter,
                      buckets: dict[int, list[str]]) -> Optional[str]:
    if not buckets:
        return None
    low = token.lower()
    if len(low) < MIN_EDIT_CANDIDATE_LEN or freq.get(low, 0) > RARE_MAX_FREQ:
        return None
    try:
        from rapidfuzz import process                       # noqa: PLC0415
        from rapidfuzz.distance import Levenshtein          # noqa: PLC0415
    except Exception:                                       # noqa: BLE001
        return None
    for length in (len(low) - 1, len(low), len(low) + 1):
        pool = buckets.get(length)
        if not pool:
            continue
        hit = process.extractOne(low, pool, scorer=Levenshtein.distance,
                                 score_cutoff=1)
        if hit and hit[0] != low and not _inflection_pair(low, hit[0]):
            return hit[0]
    return None


def flag_token(token: str, previous: Optional[str], freq: Counter,
               buckets: dict[int, list[str]]) -> Optional[tuple[str, str]]:
    """(reason, detail) when the token looks wrong, else None."""
    if previous is not None and token.lower() == previous.lower() \
            and len(token) > 2 and token.isalpha():
        return ("doubled_word", f"{previous} {token}")
    if _STRAY_PREFIX.match(token):
        return ("stray_leading_character", token)
    core = token.replace("'", "").replace("’", "")
    if core.isalpha() and not _LEGIT_MIXED.match(token):
        return ("internal_capital", token)
    if _TRIPLE.search(token):
        return ("triple_letter_run", token)
    if core.isalpha() and len(core) >= 4 and not _VOWEL.search(core) \
            and not core.isupper():
        return ("no_vowel", token)
    near = _rare_near_common(token, freq, buckets)
    if near is not None:
        return ("rare_near_common", f"{token} ~ {near}")
    return None


def section_signal(part: str, section_path: str, text: str, freq: Counter,
                   buckets: dict[int, list[str]]) -> SectionSignal:
    sig = SectionSignal(part=part, section_path=section_path, tokens=0, flagged=0)
    previous: Optional[str] = None
    for offset, token in word_tokens(text):
        sig.tokens += 1
        hit = flag_token(token, previous, freq, buckets)
        previous = token
        if hit is None:
            continue
        reason, detail = hit
        sig.flagged += 1
        sig.reasons[reason] += 1
        if len(sig.examples) < 10:
            sig.examples.append({"offset": offset, "token": token,
                                 "reason": reason, "detail": detail})
    return sig


@dataclass
class TypoSignal:
    threshold: float
    sections: dict[str, SectionSignal]          # section path -> signal
    section_of_node: dict[str, str]             # node id -> section path
    corpus_tokens: int = 0
    edit_check_ran: bool = True

    def is_typo_dense(self, node_id: str) -> bool:
        path = self.section_of_node.get(node_id)
        if path is None:
            return False
        sig = self.sections.get(path)
        return bool(sig and sig.density >= self.threshold)

    def dense_sections(self) -> list[SectionSignal]:
        return [s for s in self.sections.values() if s.density >= self.threshold]

    def as_dict(self) -> dict:
        detectors = ["stray_leading_character", "internal_capital",
                     "triple_letter_run", "no_vowel", "doubled_word"]
        if self.edit_check_ran:
            detectors.append("rare_near_common")
        return {
            "threshold": self.threshold,
            "corpus_word_tokens": self.corpus_tokens,
            "sections_scored": len(self.sections),
            "sections_typo_dense": len(self.dense_sections()),
            "detectors": sorted(detectors),
            "rare_near_common": {
                "ran": self.edit_check_ran,
                "min_corpus_word_tokens": MIN_CORPUS_TOKENS_FOR_EDIT_CHECK,
                "note": ("the once-seen-token detector needs a corpus large enough "
                         "for ordinary vocabulary to have recurred; below the "
                         "threshold it is switched off rather than guessing"),
            },
            "sections": [s.as_dict(self.threshold)
                         for s in sorted(self.sections.values(),
                                         key=lambda s: (-s.density, s.section_path))],
        }


def compute(trees: treeio.Trees, threshold: float) -> TypoSignal:
    freq = corpus_frequencies(trees)
    corpus_tokens = sum(freq.values())
    edit_check = corpus_tokens >= MIN_CORPUS_TOKENS_FOR_EDIT_CHECK
    buckets = _near_common_index(freq) if edit_check else {}
    sections: dict[str, SectionSignal] = {}
    section_of_node: dict[str, str] = {}
    for pid, part in trees.ordered():
        section_of_node.update(treeio.section_of(part))
        for sec in treeio.sections(part):
            text = treeio.subtree_text(sec.node)
            sections[sec.path] = section_signal(pid, sec.path, text, freq, buckets)
    return TypoSignal(threshold=threshold, sections=sections,
                      section_of_node=section_of_node,
                      corpus_tokens=corpus_tokens, edit_check_ran=edit_check)
