"""Stage 1, deterministic PyMuPDF parsing to blocks with boxes.

Pure function of the PDF bytes and `config.py`. No LLM calls, no timestamps in
content, no dict-ordering leaks: the same PDF produces byte-identical layout
files on every run.
"""
from .document import DocumentScan, scan
from .layout import build_layout
from .numbering import Rulebook

__all__ = ["DocumentScan", "scan", "build_layout", "Rulebook"]
