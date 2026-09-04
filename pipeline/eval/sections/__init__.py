"""The ten report sections named in handover/SPEC.md 2.6.

Section names are the contract. `pipeline/eval/report.py` asserts that the
built report carries exactly these keys, in this order, so a rename cannot
happen by accident.
"""
SECTION_NAMES = [
    "invariants",
    "page_map_vs_provided",
    "outline_vs_provided",
    "definitions_vs_provided",
    "golden_refs",
    "golden_terms",
    "stratified_audit",
    "confidence_calibration",
    "resolution_transitions",
    "concepts",
]
