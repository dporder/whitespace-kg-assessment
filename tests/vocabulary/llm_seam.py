"""Standing a fake in for `pipeline.llm`, and making that airtight.

**The bug this exists to prevent.** `pipeline/vocabulary/llmio.py` reaches the
client with `from pipeline import llm`. Once *any* test in the run has genuinely
imported that module, Python binds it as an attribute on the `pipeline` package,
and `from pipeline import llm` reads the attribute without ever consulting
`sys.modules`. A test that patches only `sys.modules` therefore passes when run
alone and is silently bypassed in a full run, in both directions: a fake is
ignored, and so is an absence. The failure mode is not just red tests, it is a
test suite quietly making real, billable API calls.

So every helper here patches **both** places, and the default is hermetic rather
than opt-in:

* `no_live_llm` is an autouse fixture that stands a tripwire in front of the real
  module for every test in the enrichment suites. A test that reaches the client
  without installing a fake fails with a message saying so, instead of spending
  money. Hermeticity stops depending on each test remembering.
* `install_llm` puts a fake in both places.
* `without_llm` removes it from both places, which is what a test asserting the
  pending-`llm.py` path actually needs.

This module lives under `tests/vocabulary/` and is imported by the concepts and
embeddings suites too, mirroring the production dependency: both of those stages
import `pipeline.vocabulary.llmio` for the same seam.
"""
from __future__ import annotations

import sys
import types

import pytest


def _bind(monkeypatch, module) -> types.ModuleType:
    """Install `module` as `pipeline.llm` in both places Python looks."""
    import pipeline
    monkeypatch.setitem(sys.modules, "pipeline.llm", module)
    monkeypatch.setattr(pipeline, "llm", module, raising=False)
    return module


def install_llm(monkeypatch, complete) -> types.ModuleType:
    """Stand a fake `pipeline.llm` in front of the real one.

    `complete` is the callable the seam will reach, matching the pinned
    contract `complete(task, prompt) -> str`.
    """
    module = types.ModuleType("pipeline.llm")
    module.complete = complete
    return _bind(monkeypatch, module)


def without_llm(monkeypatch) -> None:
    """Make `pipeline.llm` genuinely unimportable, both ways in.

    `sys.modules[...] = None` alone is not enough once the real module has been
    imported: the package attribute still resolves and the test silently
    exercises the present path it was written to rule out.
    """
    import pipeline
    monkeypatch.setitem(sys.modules, "pipeline.llm", None)
    monkeypatch.delattr(pipeline, "llm", raising=False)


class LiveLLMReached(AssertionError):
    """A test reached the real client instead of a fake."""


@pytest.fixture(autouse=True)
def no_live_llm(monkeypatch):
    """Hermetic by default: no test in these suites may reach the real client.

    Autouse, so a new test cannot forget. A test that wants a fake installs one
    over the top with `install_llm`; a test that wants absence calls
    `without_llm`. Anything else that reaches the seam fails loudly and for free.
    """
    def forbidden(*_args, **_kwargs):
        raise LiveLLMReached(
            "this test reached the real pipeline.llm. Install a fake with "
            "install_llm(monkeypatch, ...) or assert absence with "
            "without_llm(monkeypatch). Tests never call a live model.")

    tripwire = types.ModuleType("pipeline.llm")
    tripwire.complete = forbidden
    tripwire.structured = forbidden
    tripwire.message = forbidden
    _bind(monkeypatch, tripwire)
    return tripwire
