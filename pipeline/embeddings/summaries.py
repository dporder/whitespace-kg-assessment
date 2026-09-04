"""Generated summaries for the altitudes raw text cannot serve.

Whole documents, whole parts, and containers too long to embed directly get a
short generated summary (SPEC 2.4). Two rules keep this out of the trust
gradient, and both are enforced here rather than assumed:

* a summary is generated text, so its `EmbeddingRecord` carries
  `llm_derived: true`, and
* a retrieval hit on a summary vector has to resolve down to a citable leaf
  before anything is quoted, which is why the summary is never written back onto
  the node and never becomes a node's `text`.

The model is `config.MODELS["summaries"]` (Claude Haiku 4.5, "compression, not
reasoning") through `pipeline/llm.py`. When that module is absent the summary is
not invented: the item is left in the pending list with its prompt, and one
rerun completes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipeline.embeddings.plan import PlanItem
from pipeline.vocabulary.llmio import Runner

TASK = "summaries"
PROMPT_VERSION = "summary-v1"

# What the model is shown. Long enough to summarise a whole part, bounded so one
# runaway container cannot blow the context window.
SOURCE_CLIP = 24_000
MAX_WORDS = 80

PROMPT = (
    "Summarise the following provision from a UK public-sector framework "
    "agreement so that a search engine can find it by meaning.\n\n"
    "Rules:\n"
    f"- At most {MAX_WORDS} words, one paragraph, no preamble and no heading.\n"
    "- Name the parties, obligations and subject matter in the language the "
    "provision itself uses. Keep every capitalised defined term exactly as "
    "written, including its capitalisation.\n"
    "- Describe what the provision does. Do not evaluate it, do not advise, and "
    "do not add anything the text does not say.\n"
    "- If the text is a form or a table, say what it records rather than "
    "listing every row.\n"
    "- Reply with the summary text and nothing else.\n\n"
    "Provision {path} ({kind}):\n{source}")


@dataclass
class SummaryOutcome:
    item: PlanItem
    text: Optional[str]
    state: str
    note: str


def _clip(text: str) -> str:
    return text if len(text) <= SOURCE_CLIP else text[:SOURCE_CLIP] + " […truncated]"


def build_prompt(item: PlanItem) -> str:
    return PROMPT.format(path=item.path, kind=item.kind,
                         source=_clip(item.summary_source))


def generate(items: list[PlanItem], runner: Runner) -> list[SummaryOutcome]:
    """One call per item, cached on the exact prompt so reruns are free."""
    out: list[SummaryOutcome] = []
    for item in items:
        call = runner.complete(TASK, PROMPT_VERSION, build_prompt(item))
        text = (call.response or "").strip() if call.ok else None
        if text:
            item.text = text
        out.append(SummaryOutcome(item=item, text=text, state=call.state,
                                  note=call.note))
    return out
