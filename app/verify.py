"""Verification gate applied to every final answer, whatever planner produced it.

An answer is released only if:

1. it cites at least one claim, and every cited claim id was actually returned
   by a tool during this run (no invented citations);
2. every monetary amount in the answer text matches a value in a cited claim
   (current value, previous value, or the size of a restatement), allowing only
   for the rounding implied by the number of decimals shown;
3. for an as-of request, every cited claim was published on or before that date;
4. when the question asks for the current (or as-of) figure, every cited claim is
   the latest version visible at the effective date. Without this check an answer
   that presents a superseded figure as current passes 1-3: the amount matches a
   real, retrieved claim. Questions about the original figure or the history may
   cite earlier versions;
5. for the same current and as-of questions, every amount plays the role it
   claims. An amount introduced as the earlier figure ("previous", "originally",
   "restated from") must equal a cited claim's previous value; one introduced as
   a change ("down", "up by") must equal the size of the restatement; any other
   amount is a headline figure and must equal the value of a cited latest
   version. Without this check, "the current revenue is $120 million" citing the
   restated claim passes 1-4, because $120 million is that claim's previous value;
6. every cited claim belongs to the company, metric and fiscal period the
   question names (where the question names them). Without this check a
   correctly cited ACME figure passes as the answer to a question about Initech.

Otherwise the answer is replaced by an abstention that lists the failed checks.
Each check can be switched off individually (``checks``) so the red-team
evaluation can measure which attack each one stops.
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
    re.compile(r"\b(?:FY\s?\d{4}|Q[1-4][\s-]*(?:FY)?\s?\d{4})\b", re.I),  # fiscal periods
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
    prefix: str = ""  # text between the previous amount (or the start) and this one


def extract_amounts(text: str) -> list[Amount]:
    cleaned = text
    for pattern in _STRIP:
        cleaned = pattern.sub(" ", cleaned)
    out = []
    prev_end = 0
    for m in _NUMBER.finditer(cleaned):
        num = m.group("num").replace(",", "")
        scale = SCALES.get((m.group("scale") or "").lower(), Decimal(1))
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = Decimal(num) * scale
        tolerance = (Decimal(1) / (Decimal(10) ** decimals)) / 2 * scale if scale != 1 else Decimal("0.005")
        out.append(Amount(m.group(0).strip(), value, tolerance, cleaned[prev_end:m.start()]))
        prev_end = m.end()
    return out


_EARLIER = re.compile(r"\b(?:previous(?:ly)?|prior|original(?:ly)?|initial(?:ly)?|earlier|restated from)\b"
                      r"|\bfrom\s*$", re.I)
_CHANGE = re.compile(r"\b(?:down|up|lower|higher|(?:a )?(?:decrease|increase|reduction|change|restatement) of)"
                     r"(?:\s+by)?\s*$|\bby\s*$", re.I)


def amount_role(amount: Amount) -> str:
    """'change', 'earlier' or 'headline', from the words that introduce the amount."""
    if _CHANGE.search(amount.prefix):
        return "change"
    if _EARLIER.search(amount.prefix):
        return "earlier"
    return "headline"


def _allowed_values(cited: list[dict[str, Any]]) -> list[Decimal]:
    vals: list[Decimal] = []
    for c in cited:
        for key in ("value", "previous_value", "change"):
            if c.get(key) is not None:
                vals.append(abs(Decimal(str(c[key]))))
    return vals


CHECKS = frozenset({"citations", "amounts", "as_of", "currency", "roles", "scope"})
LatestIndex = dict[tuple[str, str, str], int]
Scope = dict[str, set[str]]  # field -> allowed values, for the fields the question names


@dataclass
class Verification:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checked_amounts: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)  # names from CHECKS, in order


def claim_key(claim: dict[str, Any]) -> tuple[str, str, str]:
    return (str(claim.get("ticker")), str(claim.get("metric")), str(claim.get("fiscal_period")))


def verify_answer(
    answer: str,
    citations: list[int],
    evidence: dict[int, dict[str, Any]],
    as_of: date | None = None,
    latest: LatestIndex | None = None,
    scope: Scope | None = None,
    checks: frozenset[str] = CHECKS,
) -> Verification:
    """Check a proposed answer. ``latest`` maps (ticker, metric, period) to the id of
    the latest version visible at the effective date; pass it when the question asks
    for the current or as-of figure, and leave it None for original/history questions.
    ``scope`` restricts the cited claims to the entities the question names."""
    failures: list[str] = []
    failed: list[str] = []

    def fail(check: str, message: str) -> None:
        failures.append(message)
        if check not in failed:
            failed.append(check)

    cited = [evidence[c] for c in citations if c in evidence]
    if "citations" in checks:
        if not citations:
            fail("citations", "no citations")
        unknown = [c for c in citations if c not in evidence]
        if unknown:
            fail("citations", f"cited claim ids not returned by any tool: {unknown}")
    amounts = extract_amounts(answer)
    if "amounts" in checks:
        allowed = _allowed_values(cited)
        for amt in amounts:
            if not any(abs(abs(amt.value) - v) <= amt.tolerance for v in allowed):
                fail("amounts", f"amount {amt.text!r} does not match any cited claim")
    if "as_of" in checks and as_of is not None:
        late = [c["claim_id"] for c in cited if date.fromisoformat(c["filed_at"]) > as_of]
        if late:
            fail("as_of", f"cited claims published after as_of {as_of}: {late}")
    if "currency" in checks and latest is not None:
        stale = [(c["claim_id"], latest[claim_key(c)]) for c in cited
                 if claim_key(c) in latest and c["claim_id"] != latest[claim_key(c)]]
        if stale:
            fail("currency", "cited superseded versions (cited id -> latest visible id): "
                 + ", ".join(f"{a} -> {b}" for a, b in stale))
    if "roles" in checks and latest is not None:
        current = [c for c in cited if latest.get(claim_key(c)) == c["claim_id"]]
        role_values = {
            "headline": [abs(Decimal(str(c["value"]))) for c in current],
            "earlier": [abs(Decimal(str(c["previous_value"]))) for c in cited if c.get("previous_value") is not None],
            "change": [abs(Decimal(str(c["change"]))) for c in cited if c.get("change") is not None],
        }
        for amt in amounts:
            role = amount_role(amt)
            if not any(abs(abs(amt.value) - v) <= amt.tolerance for v in role_values[role]):
                fail("roles", f"amount {amt.text!r} is presented as the {role} figure but does not "
                              f"match the {role} value of a cited latest claim")
    if "scope" in checks and scope:
        outside = [c["claim_id"] for c in cited
                   if any(str(c.get(f)) not in allowed for f, allowed in scope.items())]
        if outside:
            named = ", ".join(f"{f}={'/'.join(sorted(v))}" for f, v in sorted(scope.items()))
            fail("scope", f"cited claims outside the question's scope ({named}): {outside}")
    return Verification(not failures, failures, [a.text for a in amounts], failed)
