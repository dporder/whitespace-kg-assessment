"""Reporting primitives: a rate is never printed without its counts.

`Rate(9, 10)` renders as "9/10 (0.900)" in markdown and as
`{"count": 9, "of": 10, "rate": 0.9}` in JSON. `Rate(0, 0)` renders as
"0/0 (no data)" with `rate: null`, because a rate over an empty denominator is
not zero and not one, it is unknown. SPEC section 5: "the harness reports
absolute counts alongside rates so nobody mistakes 9 of 10 for 900 of 1000".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

MEASURED = "measured"
PARTIAL = "partial"
NO_DATA = "no_data"
ERROR = "error"


@dataclass(frozen=True)
class Rate:
    """A ratio that cannot be printed without its absolute counts."""
    count: int
    of: int

    @property
    def rate(self) -> Optional[float]:
        if self.of == 0:
            return None
        return self.count / self.of

    @property
    def has_data(self) -> bool:
        return self.of > 0

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "of": self.of, "rate": self.rate}

    def __str__(self) -> str:
        if self.of == 0:
            return "0/0 (no data)"
        return f"{self.count}/{self.of} ({self.rate:.3f})"


def rate_of(numerator: Iterable[Any] | int, denominator: Iterable[Any] | int) -> Rate:
    """Rate from either counts or the collections themselves."""
    n = numerator if isinstance(numerator, int) else len(list(numerator))
    d = denominator if isinstance(denominator, int) else len(list(denominator))
    return Rate(n, d)


@dataclass
class Section:
    """One SPEC 2.6 report section.

    `name` is the exact section name from SPEC 2.6 and is used verbatim as the
    JSON key and the markdown heading. `status` distinguishes a measurement
    from an absence: `measured` (ran on real input), `partial` (ran on some of
    it, `reason` says what was missing), `no_data` (nothing to measure on) and
    `error` (the check itself failed). `metrics` publishes named values for
    `gates.py`; nothing else reads them.
    """
    name: str
    status: str = NO_DATA
    reason: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)
    md: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.reason:
            out["reason"] = self.reason
        out.update(self.data)
        return out

    def line(self, text: str = "") -> None:
        self.md.append(text)

    def bullet(self, text: str) -> None:
        self.md.append(f"- {text}")

    def table(self, headers: list[str], rows: list[list[Any]]) -> None:
        """Markdown table.

        Empty rows print an explicit 'none' rather than an empty table, and a
        None cell prints an em dash rather than a blank, so an absent value
        never reads as a zero. Pipes inside cells are escaped, otherwise a
        stratum key like "1 word | core-terms" silently breaks the table.
        """
        def cell(value: Any) -> str:
            if value is None:
                return "—"
            return str(value).replace("|", "\\|")

        if not rows:
            self.md.append("_none_")
            self.md.append("")
            return
        self.md.append("| " + " | ".join(cell(h) for h in headers) + " |")
        self.md.append("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            self.md.append("| " + " | ".join(cell(c) for c in row) + " |")
        self.md.append("")


def cap(items: list[Any], limit: int) -> tuple[list[Any], int]:
    """Truncate a listing for the report, returning what was hidden.

    The count is always the full count; only the listing is capped, so a large
    golden set never makes the harness print a misleading total.
    """
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit
