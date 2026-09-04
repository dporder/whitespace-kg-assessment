"""Stage 8, the evaluation harness. Owned by eval-builder.

`python -m pipeline.eval [--full]` writes output/<run>/eval/report.json and
report.md with the sections named in handover/SPEC.md 2.6, and exits 2 when a
gate in config.GATES fails.

Three rules run through every module here:

1. **Absolute counts beside every rate.** Every ratio is a `Rate`, which
   carries its numerator and denominator and renders "9/10 (0.900)". A rate
   over an empty denominator is `None`, never 0.0 and never 1.0.
2. **Absent is not failed.** Each section reports `measured`, `partial`,
   `no_data` or `error` with a reason. A gate never fires on missing data, it
   is recorded as `skipped_no_data`.
3. **Scale independence.** Nothing assumes how many golden labels exist. The
   harness behaves identically at ten labels and at ten thousand.
"""
