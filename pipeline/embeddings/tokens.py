"""A deterministic token estimate for the subtree budget.

`config.SUBTREE_EMBED_TOKEN_BUDGET` decides whether a container is embedded on
its own concatenated text or on a generated summary, so the count has to be the
same on every machine and every run. The repo ships no tokenizer for the OpenAI
model (`tiktoken` is not among the installed dependencies) and downloading one is
not available inside an air-gapped boundary, so this is an explicit, documented
approximation rather than a hidden one:

* a word or number costs `ceil(len / 4)` tokens, which is the byte-pair rate for
  English prose,
* every punctuation mark costs one.

It runs slightly high on long legal words and slightly low on heavily
punctuated lists, and it is stable, which is the property the budget needs. If
an exact tokenizer is ever added, swapping it in behind `estimate_tokens` is a
one-function change; the plan records `token_estimator` so a reader knows which
count a decision stood on.
"""
from __future__ import annotations

import math
import re

ESTIMATOR = "chars-per-token-4-v1"
TOKEN = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    total = 0
    for match in TOKEN.finditer(text):
        token = match.group(0)
        total += math.ceil(len(token) / 4) if token.isalnum() else 1
    return total
