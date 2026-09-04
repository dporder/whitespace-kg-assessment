"""Slice definitions, hierarchy profiles, model choices and thresholds.

Owned by the orchestrator. Workers read, never edit. Everything tunable lives here so
that no threshold is buried in code.
"""
from pathlib import Path

ROOT = Path(__file__).parent
PDF = ROOT.parent / "technical-assignment-provided-by-whitespace" / "document" / \
      "RM6116 - Network Services 3 - Framework Agreement.pdf"
OUTPUT = ROOT / "output"
GOLDEN = ROOT / "golden"
ENV_FILE = ROOT / ".env"   # gitignored; keys may also come from the process environment

DOCUMENT_ID = "rm6116"

# Four incremental batches. Loaded in order so that references out to not-yet-ingested
# parts sit unresolved and then flip as their targets arrive. Pages are 1-based inclusive.
BATCHES = {
    "B1": {"part": "core-terms",           "pages": (1, 22),    "genre": "clauses"},
    "B2": {"part": "joint-schedule-1",     "pages": (112, 139), "genre": "definitions"},
    "B3": {"part": "award-form",           "pages": (23, 30),   "genre": "form"},
    "B4": {"part": "call-off-schedule-9",  "pages": (340, 361), "genre": "clauses"},
}

# Stages 0 to 2 also run across every page with no LLM cost, to derive the page map,
# diff the outline, and report per-part invariant violations.
FULL_STRUCTURAL_PAGES = (1, 475)

HIERARCHY_PROFILES = {
    "uk-ccs-framework": {
        "levels": ["part", "heading", "clause", "subclause", "item"],
        "numbering": {
            "heading":  r"^\s{0,4}(\d{1,2})\.\s+(?=[A-Z])",
            "clause":   r"^\s{0,10}(\d{1,2}\.\d{1,2})\s+",
            "subclause": r"^\s{0,12}(\d{1,2}\.\d{1,2}\.\d{1,2})\s+",
            "item":     r"^\s+\(([a-z]{1,2}|(?:x{0,3})(?:ix|iv|v?i{0,3}))\)\s",
        },
        "max_dotted_depth": 3,          # verified across all 475 pages, zero four-level numbers
        "unit_labels": {"core-terms": "Clause", "_schedule_default": "Paragraph"},
        "unit_labels_from_document": ["Clause", "Schedule", "Part", "Paragraph", "Annex", "Table"],
        "unit_labels_from_profile": ["item"],   # interpretation clause is silent on (a) and (i)
        "interpretation_cues": [
            r"unless the context otherwise requires",
            r"[Ii]n this Schedule[,:]?\s",
            r"references to\b",
        ],
        "supports_wrapup": True,        # capability, unused by this document
        "citable_kinds": ["part", "heading", "clause", "subclause", "item", "form_row", "table"],
    },
}
DEFAULT_PROFILE = "uk-ccs-framework"

# Any one of these firing quarantines the document rather than ingesting a guessed tree.
QUARANTINE_THRESHOLDS = {
    "max_unmatched_numbering_rate": 0.05,   # lines with numbering the grammar does not cover
    "max_orphan_block_rate":        0.10,   # text blocks attaching to no node
    "max_geometry_disagreement":    0.05,   # indent-implied depth vs numbering-implied depth
    "require_interpretation_clause": True,
}

MODELS = {
    "reference_residue": "claude-haiku-4-5",
    "reference_hard":    "claude-sonnet-5",
    "concepts":          "claude-sonnet-5",
    "summaries":         "claude-haiku-4-5",
    "eval_judge":        "claude-haiku-4-5",
    "chat_agent":        "claude-opus-5",
    "chat_gate":         "claude-haiku-4-5",
    "chat_plan":         "claude-opus-5",
}
EMBEDDING_MODEL = "text-embedding-3-large"   # OpenAI, via OPENAI_API_KEY; swap for an in-boundary
# model in sovereign deployments, vectors live outside the graph so that swap is a re-embed
SUBTREE_EMBED_TOKEN_BUDGET = 512
LEAF_WINDOW_EMBEDDING = False        # A/B variant: embed leaf with prev+next sibling; replaces leaf_text
CONCEPT_MERGE_COSINE = 0.80          # near-duplicate concept resolution threshold
ASSOCIATED_TERM_MIN_SHARE = 0.25     # min share of a concept's provisions using a term for the edge

SALIENCE = {                          # salience = breadth * log(1 + frequency); boost = w * log(1 + salience)
    "retrieval_boost_weight": 0.02,
}

ERROR_COSTS = {                       # placeholders for a domain expert; used in the cost-weighted
    "term_false_positive": 1.0,       # confusion summary, not in matching itself
    "term_false_negative": 3.0,
}
TYPO_DENSITY_THRESHOLD = 0.02        # share of misspelled tokens per section that triggers typo_dense routing

GATES = {
    "reference_precision_min": 0.90,     # on golden resolved references
    "wrongly_resolved_unresolvables_max": 0,   # abstention is scored
    "structural_violations_unexplained_max": 0,
    "detection_recall_min": 0.95,
    "stratified_audit_agreement_min": 0.90,
}

AUDIT = {
    "confident_term_sample_size": 40,
    "strata": ["term_word_count", "part", "position"],
}

NEO4J = {"uri": "bolt://localhost:7687", "user": "neo4j", "database": "neo4j"}
