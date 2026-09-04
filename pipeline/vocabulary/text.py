"""Text primitives for the vocabulary stage. Nothing here alters stored text.

Two jobs are kept strictly apart, because confusing them is how a fidelity rule
gets broken by accident:

* **Keying.** `term_key` turns a raw term cell into the string used as a map key
  and as `DefinitionSite.term`. It strips the quote marks the drafters put
  round the term and collapses the whitespace that a wrapped cell introduces.
  It never repairs, completes or re-cases anything. Joint Schedule 1 prints 206
  term cells on the batch-B2 pages with a closing quote and no opening one, and
  a dozen whose first letter is genuinely absent from the page (`nsurances`,
  `ncorporated Terms`). Those keys stay exactly as printed, missing letter and
  all, and the defect is recorded as an anomaly beside them.
* **Reading.** Everything else here inspects text and returns offsets or
  booleans. No function in this module returns a modified copy of a node's text.
"""
from __future__ import annotations

import re
import unicodedata

# Quote marks the pack actually uses, measured on the definitions pages:
# 263 ASCII ", 36 U+201D, 10 U+201C, 38 U+2019, 1 U+2018.
QUOTES = "\"“”‘’«»„‟"
OPEN_QUOTES = "\"“‘«„"
CLOSE_QUOTES = "\"”’»‟"

# A word token for the typo-density signal. Hyphens and apostrophes are word
# internal in this document ("Call-Off", "Supplier's"), so they join rather
# than split, but only between letters.
WORD = re.compile(r"[A-Za-z](?:[A-Za-z]|['’](?=[A-Za-z])|-(?=[A-Za-z]))*")

# Sentence boundary, identical to the rule pipeline/eval/sections/definitions.py
# applies, so stage 4 and stage 8 cannot disagree about what "sentence initial"
# means. tests/vocabulary/test_ambiguity.py pins the two together.
_SENTENCE_END = re.compile(r"[.;:!?]\s*$")

# A capitalised phrase: title-case words, allowing the lowercase joining words
# legal drafting puts inside term names ("Freedom of Information Act").
_JOINERS = {"of", "the", "and", "or", "for", "to", "in", "on", "a", "an", "de"}


def strip_quotes(raw: str) -> str:
    """Remove one leading and one trailing quote mark, if present.

    Deliberately one each, not a strip of the whole class: a term whose printed
    form ends in a quote and starts without one (the JS1 defect) must lose only
    the quote that is there.
    """
    s = raw.strip()
    if s and s[0] in OPEN_QUOTES:
        s = s[1:]
    if s and s[-1] in CLOSE_QUOTES:
        s = s[:-1]
    return s.strip()


def collapse_ws(raw: str) -> str:
    """Whitespace runs, including the newlines a wrapped cell introduces, to
    one space. Key normalisation only (the same rule schemas.normalise_for_hash
    applies for content hashes)."""
    return " ".join(raw.split())


def term_key(raw: str) -> str:
    """The key form of a printed term. Keying only, never a repair."""
    s = unicodedata.normalize("NFC", raw)
    s = collapse_ws(s)
    s = strip_quotes(s)
    # A term cell that ends in the list punctuation of its own row.
    s = s.rstrip(";,").strip()
    return strip_quotes(s)


def quote_shape(raw: str) -> str:
    """Which quote marks a printed term actually carries: both, closing_only,
    opening_only or none. Recorded as an anomaly, never corrected."""
    s = collapse_ws(raw).rstrip(";,").strip()
    if not s:
        return "none"
    opens = s[0] in OPEN_QUOTES
    closes = s[-1] in CLOSE_QUOTES and len(s) > 1
    if opens and closes:
        return "both"
    if closes:
        return "closing_only"
    if opens:
        return "opening_only"
    return "none"


def looks_like_term(key: str) -> bool:
    """Is this key plausibly a defined term as this family draws them?

    Capitalised first character, or the known missing-first-letter defect, and
    short enough to be a name rather than a sentence. Deliberately permissive:
    the caller decides using context (a definitions table, a `means` verb)
    whether a definition is really being made, and this only rejects the
    obviously-not.
    """
    if not key or len(key) > 120:
        return False
    if key.endswith((".", ":")):
        return False
    words = key.split()
    if not words or len(words) > 12:
        return False
    return bool(re.match(r"^[A-Za-z][\w\-'’()/&., ]*$", key))


def is_capitalised_phrase(key: str) -> bool:
    """Every word title-cased or an accepted lowercase joiner, first word
    capitalised. The discovery rule's shape test.

    Quote marks and brackets are stripped per word before the test, because the
    phrase reaching here is often still wearing the drafters' quotation marks.
    """
    words = [w.strip("()[]“”\"'’.,;:") for w in key.split()]
    words = [w for w in words if w]
    if not words:
        return False
    if not words[0][:1].isupper():
        return False
    for core in words:
        if core[:1].isupper():
            continue
        if core.lower() in _JOINERS:
            continue
        return False
    return True


def sentence_initial(field: str, start: int) -> bool:
    """Is the match at `start` in sentence-initial position?

    True at the very start of the field, and after a sentence-ending mark. The
    field start counts because a lettered item's text begins mid-sentence in
    the source but at offset 0 here, and a capital there is exactly as weak as
    one after a full stop.
    """
    before = field[:start].rstrip()
    return not before or bool(_SENTENCE_END.search(before))


def initials(phrase: str) -> str:
    """Initial letters of the significant words, for the alias test.

    "Information and Communication Technology" -> "ICT" (joiners skipped),
    "Central Buying Office" -> "CBO".
    """
    out = []
    for w in phrase.split():
        core = w.strip("()[]“”\"'’.,;:-")
        if not core:
            continue
        if core.lower() in _JOINERS and out:
            continue
        out.append(core[0].upper())
    return "".join(out)


def is_initialism_of(candidate: str, phrase: str) -> bool:
    """Is `candidate` an abbreviation of the phrase immediately before it?

    This is the rule that separates the two parenthetical conventions the pack
    uses. `Information and Communication Technology ("ICT")` introduces an
    alias for the phrase in front of it. `... provision of the EEA agreement
    ("EU References")` introduces a term in its own right. The initialism test
    is what tells them apart deterministically, with no model and no list.
    """
    cand = re.sub(r"[^A-Za-z]", "", candidate)
    if not cand or not cand.isupper() or len(cand) < 2:
        return False
    words = phrase.split()
    # Look back over exactly as many words as the abbreviation has letters,
    # then widen by the joiners that abbreviation conventions skip.
    for window in range(len(cand), min(len(cand) * 2 + 2, len(words)) + 1):
        tail = " ".join(words[-window:])
        if initials(tail) == cand:
            return True
    return False


def word_tokens(text: str) -> list[tuple[int, str]]:
    """(offset, token) for every word token. Deterministic."""
    return [(m.start(), m.group(0)) for m in WORD.finditer(text)]


def word_count_bucket(text: str) -> str:
    """Audit stratum. Identical to pipeline/eval/sampling.word_count_bucket;
    tests/vocabulary/test_audit.py asserts the two agree."""
    words = len(text.split())
    if words <= 1:
        return "1 word"
    if words == 2:
        return "2 words"
    return "3+ words"


def position_bucket(order: int, total: int) -> str:
    """Audit stratum. Identical to pipeline/eval/sampling.position_bucket."""
    if total <= 1:
        return "only"
    frac = order / max(total - 1, 1)
    if frac < 1 / 3:
        return "early"
    if frac < 2 / 3:
        return "middle"
    return "late"
