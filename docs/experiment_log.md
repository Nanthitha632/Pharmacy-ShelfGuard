# Pharmacy ShelfGuard — experiment log

All records and outcomes are **synthetic**. This document distinguishes
completed results from resume targets.

## Business question
How can a pharmacy network serve more customer requests while keeping
inventory value under control?

## Evidence so far

| Experiment | Fill rate | Stockout store–SKU days | Average daily inventory value |
|---|---:|---:|---:|
| Baseline, 15-day test | 91.82% | 1,778 | $445,328.43 |
| Broad safety stock pilot | 97.96% | 459 | $594,109.34 |
| Baseline plus transfers | 91.77% | 1,843 | $443,705.39 |

The full 60-day baseline fill rate was 91.0%.
Its time window differs from the 15-day comparison above; do not
compare those percentages as if they covered the same dates.

## What we learned

1. Demand data passed integrity checks before use.
2. The held-out forecast had 22.28% WAPE and
   -0.45% bias.
3. The first replay omitted the February 14 recommendations. It is
   retained as a debugging experiment, **not** the final evaluation.
4. The corrected broad safety stock pilot raised inventory value by
   33.41%. Decision: reject
   it under the 2% inventory limit.
5. The transfer experiment changed fill rate by
   -0.05 percentage points.
   Decision: reject the current donor rule.

## Target and status

Resume target: 120,000 source event records; fill rate 92.0% to
96.1%; stockout days down 18%; average inventory value up no more
than 2%. **These results have not yet been demonstrated.**

## Next design decision

Build one consistent supplier-event and replay model. Test a
selective replenishment rule against the same 15-day demand and
inventory-value constraint. Record the parameters before running the
final evaluation. Keep exploratory trials separate from final claims.
