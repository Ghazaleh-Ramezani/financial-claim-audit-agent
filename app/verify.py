"""Verification gate applied to every final answer, whatever planner produced it.

An answer is released only if:

1. it cites at least one claim, and every cited claim id was actually returned
   by a tool during this run (no invented citations);
2. every monetary amount in the answer text matches a value in a cited claim
   (current value, previous value, or the size of a restatement), allowing only
   for the rounding implied by the number of decimals shown;
3. for an as-of request, every cited claim was published on or before that date.

Otherwise the answer is replaced by an abstention that lists the failed checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

SCALES = {
    "thousand": Decimal(10) ** 3, "k": Decimal(10) ** 3,
    "million": Decimal(10) ** 6, "m": Decimal(10) ** 6, "mn": Decimal(10) ** 6,
    "billion": Decimal(10) ** 9, "b": Decimal(10) ** 9, "bn": Decimal(10) ** 9,
}

_STRIP = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),          # ISO dates
    re.compile(r"\bSYN-[A-Z0-9-]+\b"),              # accession numbers
    re.compile(r"\b(?:FY\d{4}|Q[1-4]-\d{4})\b"),    # fiscal periods
    re.compile(r"\b10-[KQ](?:/A)?\b"),              # form types
    re.compile(r"\bclaim[_ ]?(?:id)?\s*#?\d+\b", re.I),
    re.compile(r"(?<![$\d.,])\b(?:19|20)\d{2}\b(?![\d,.]*\s*(?:thousand|million|billion))"),
    re.compile(r"\b(?:version|v)\s*\d+\b", re.I),
]
_NUMBER = re.compile(
    r"(?P<neg>[-(])?\s*\$?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>thousand|million|billion|bn|mn|[kmb])?\b",
    re.I,
)


@dataclass
class Amount:
    text: str
    value: Decimal
    tolerance: Decimal


def extract_amounts(text: str) -> list[Amount]:
    cleaned = text
    for pattern in _STRIP:
        cleaned = pattern.sub(" ", cleaned)
    out = []
    for m in _NUMBER.finditer(cleaned):
        num = m.group("num").replace(",", "")
        scale = SCALES.get((m.group("scale") or "").lower(), Decimal(1))
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = Decimal(num) * scale
        tolerance = (Decimal(1) / (Decimal(10) ** decimals)) / 2 * scale if scale != 1 else Decimal("0.005")
        out.append(Amount(m.group(0).strip(), value, tolerance))
    return out


def _allowed_values(cited: list[dict[str, Any]]) -> list[Decimal]:
    vals: list[Decimal] = []
    for c in cited:
        for key in ("value", "previous_value", "change"):
            if c.get(key) is not None:
                vals.append(abs(Decimal(str(c[key]))))
    return vals


@dataclass
class Verification:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checked_amounts: list[str] = field(default_factory=list)


def verify_answer(
    answer: str,
    citations: list[int],
    evidence: dict[int, dict[str, Any]],
    as_of: date | None = None,
) -> Verification:
    failures: list[str] = []
    if not citations:
        failures.append("no citations")
    unknown = [c for c in citations if c not in evidence]
    if unknown:
        failures.append(f"cited claim ids not returned by any tool: {unknown}")
    cited = [evidence[c] for c in citations if c in evidence]
    allowed = _allowed_values(cited)
    amounts = extract_amounts(answer)
    for amt in amounts:
        if not any(abs(abs(amt.value) - v) <= amt.tolerance for v in allowed):
            failures.append(f"amount {amt.text!r} does not match any cited claim")
    if as_of is not None:
        late = [c["claim_id"] for c in cited if date.fromisoformat(c["filed_at"]) > as_of]
        if late:
            failures.append(f"cited claims published after as_of {as_of}: {late}")
    return Verification(not failures, failures, [a.text for a in amounts])
