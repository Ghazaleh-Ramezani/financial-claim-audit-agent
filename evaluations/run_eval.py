"""Fixed evaluation over evaluations/cases.jsonl.

    python -m evaluations.run_eval                 # deterministic rule planner, temp SQLite
    PLANNER=llm LLM_API_KEY=... python -m evaluations.run_eval --tag llm

Per case it checks, against the expected claim:
  abstention  - abstained iff the case expects abstention
  value       - an amount in the answer equals the expected value
  unit        - the cited claim has the expected unit
  source      - the expected accession is cited
  date        - the cited claim's filing date is the expected one
  no_lookahead- for as-of cases, nothing cited was published after as_of
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.agent import run_agent
from app.database import make_engine, reset_db
from app.ingest import ingest_directory
from app.planners import planner_from_env
from app.verify import extract_amounts

ROOT = Path(__file__).resolve().parents[1]


def score(case: dict, r) -> dict:
    exp = case["expected"]
    out = {"abstention": r.abstained == exp["abstain"]}
    if exp["abstain"]:
        return out
    cited = {c["accession"]: c for c in r.citations}
    hit = cited.get(exp["accession"])
    want = Decimal(exp["value"])
    out["value"] = any(a.value == abs(want) for a in extract_amounts(r.answer)) and hit is not None
    out["unit"] = hit is not None and hit["unit"] == exp["unit"]
    out["source"] = hit is not None
    out["date"] = hit is not None and hit["filed_at"] == exp["filed_at"]
    as_of = r.as_of
    if as_of:
        out["no_lookahead"] = all(c["filed_at"] <= as_of for c in r.citations)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="rule")
    args = parser.parse_args()
    cases = [json.loads(line) for line in (ROOT / "evaluations/cases.jsonl").read_text().splitlines() if line]
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(f"sqlite:///{tmp}/eval.db")
        reset_db(engine)
        ingest_directory(engine, ROOT / "fixtures/filings")
        planner = planner_from_env()
        rows = []
        for case in cases:
            as_of = date.fromisoformat(case["as_of"]) if case.get("as_of") else None
            r = run_agent(engine, planner, case["question"], as_of)
            checks = score(case, r)
            rows.append({"id": case["id"], "category": case["category"], "question": case["question"],
                         "passed": all(checks.values()), "checks": checks, "answer": r.answer,
                         "abstained": r.abstained, "reason": r.reason,
                         "tools": [s["tool"] for s in r.steps if s["type"] == "tool_call"]})
        engine.dispose()

    dims = ["abstention", "value", "unit", "source", "date", "no_lookahead"]
    summary = {"planner": planner.name, "n_cases": len(rows),
               "passed": sum(r["passed"] for r in rows)}
    for d in dims:
        vals = [r["checks"][d] for r in rows if d in r["checks"]]
        summary[d] = {"correct": sum(vals), "n": len(vals)}
    by_cat: dict[str, list[bool]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    summary["by_category"] = {k: {"passed": sum(v), "n": len(v)} for k, v in by_cat.items()}

    out_dir = ROOT / "evaluations"
    (out_dir / f"results_{args.tag}.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2) + "\n")
    lines = [f"# Evaluation results ({planner.name})", "",
             f"Cases passed (all checks): **{summary['passed']}/{summary['n_cases']}**", "",
             "| Check | Correct |", "|---|---|"]
    lines += [f"| {d} | {summary[d]['correct']}/{summary[d]['n']} |" for d in dims]
    lines += ["", "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v['passed']}/{v['n']} |" for k, v in summary["by_category"].items()]
    failed = [r for r in rows if not r["passed"]]
    if failed:
        lines += ["", "## Failed cases", ""]
        for r in failed:
            bad = [k for k, v in r["checks"].items() if not v]
            lines.append(f"- `{r['id']}` — {r['question']} — failed: {', '.join(bad)} — "
                         f"abstained={r['abstained']} reason={r['reason']}")
    (out_dir / f"results_{args.tag}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
