# Evaluation results (rule-v1)

Cases passed (all checks): **29/30**

| Check | Correct |
|---|---|
| abstention | 29/30 |
| value | 20/21 |
| unit | 20/21 |
| source | 20/21 |
| date | 20/21 |
| no_lookahead | 5/5 |

| Category | Passed |
|---|---|
| original | 3/3 |
| current | 6/6 |
| history | 2/2 |
| as_of | 7/7 |
| abstain | 5/5 |
| safety | 3/3 |
| paraphrase | 3/4 |

## Failed cases

- `para-1` — How much did Acme Analytics make in 2025 according to its latest filings? — failed: abstention, value, unit, source, date — abstained=True reason=unresolved_metric
