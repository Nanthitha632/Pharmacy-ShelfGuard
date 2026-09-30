from pathlib import Path
import json

root = Path(__file__).resolve().parents[1]
reports = root / "reports"
output = root / "docs" / "experiment_log.md"

def read(name):
    return json.loads((reports / name).read_text(encoding="utf-8"))

baseline = read("03_baseline_metrics.json")
forecast = read("05_forecast_metrics.json")
first_pilot = read("07_policy_comparison.json")
corrected = read("08_corrected_pilot.json")
transfer = read("10_transfer_impact.json")

text = f"""# Pharmacy ShelfGuard — experiment log

All records and outcomes are **synthetic**. This document distinguishes
completed results from resume targets.

## Business question
How can a pharmacy network serve more customer requests while keeping
inventory value under control?

## Evidence so far

| Experiment | Fill rate | Stockout store–SKU days | Average daily inventory value |
|---|---:|---:|---:|
| Baseline, 15-day test | {corrected['baseline']['fill_rate_percent']}% | {corrected['baseline']['stockout_store_sku_days']:,} | ${corrected['baseline']['average_daily_inventory_value_usd']:,.2f} |
| Broad safety stock pilot | {corrected['corrected_policy_pilot']['fill_rate_percent']}% | {corrected['corrected_policy_pilot']['stockout_store_sku_days']:,} | ${corrected['corrected_policy_pilot']['average_daily_inventory_value_usd']:,.2f} |
| Baseline plus transfers | {transfer['baseline_plus_transfers']['fill_rate_percent']}% | {transfer['baseline_plus_transfers']['stockout_store_sku_days']:,} | ${transfer['baseline_plus_transfers']['average_daily_inventory_value_usd']:,.2f} |

The full 60-day baseline fill rate was {baseline['fill_rate_percent']}%.
Its time window differs from the 15-day comparison above; do not
compare those percentages as if they covered the same dates.

## What we learned

1. Demand data passed integrity checks before use.
2. The held-out forecast had {forecast['wape_percent']}% WAPE and
   {forecast['forecast_bias_percent']}% bias.
3. The first replay omitted the February 14 recommendations. It is
   retained as a debugging experiment, **not** the final evaluation.
4. The corrected broad safety stock pilot raised inventory value by
   {corrected['change']['inventory_value_percent']}%. Decision: reject
   it under the 2% inventory limit.
5. The transfer experiment changed fill rate by
   {transfer['change']['fill_rate_percentage_points']} percentage points.
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
"""

output.write_text(text, encoding="utf-8")
print(f"Saved: {output.relative_to(root)}")
print("Open the file in VS Code to see every experiment in one place.")