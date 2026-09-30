from pathlib import Path
from collections import defaultdict
from datetime import date
import csv
import json

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"
cutoff = date(2026, 2, 14)

def load(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

products = {
    row["sku"]: row for row in load(raw / "products.csv")
}
baseline = load(processed / "baseline_daily.csv")
recommendations = load(
    processed / "order_recommendations.csv"
)

# Everything used to set the budget and rank risk ends Feb 14.
training_rows = [
    row for row in baseline
    if date.fromisoformat(row["date"]) <= cutoff
]
assert len(training_rows) == 45 * 40 * 30

training_inventory_value = sum(
    int(row["closing_units"])
    * float(products[row["sku"]]["unit_cost_usd"])
    for row in training_rows
) / 45
budget = round(0.02 * training_inventory_value, 2)

past_unmet = defaultdict(int)
past_stockout_days = defaultdict(int)

for row in training_rows:
    key = (row["store_id"], row["sku"])
    unmet = int(row["unmet_units"])
    past_unmet[key] += unmet
    past_stockout_days[key] += int(unmet > 0)

candidates = []

for row in recommendations:
    units = int(row["recommended_order_units"])
    if units == 0:
        continue

    key = (row["store_id"], row["sku"])
    cost = round(
        units * float(products[row["sku"]]["unit_cost_usd"]),
        2
    )
    if cost <= 0:
        continue

    candidates.append({
        "decision_date": cutoff.isoformat(),
        "store_id": key[0],
        "sku": key[1],
        "supplier_id": row["supplier_id"],
        "order_units": units,
        "estimated_purchase_cost_usd": cost,
        "past_unmet_units": past_unmet[key],
        "past_stockout_days": past_stockout_days[key],
        "priority_score": round(past_unmet[key] / cost, 6),
    })

candidates.sort(
    key=lambda row: (
        -row["priority_score"],
        -row["past_unmet_units"],
        row["store_id"],
        row["sku"],
    )
)

selected = []
spent = 0.0

for row in candidates:
    if row["past_unmet_units"] == 0:
        continue
    if spent + row["estimated_purchase_cost_usd"] <= budget:
        selected.append(row)
        spent = round(
            spent + row["estimated_purchase_cost_usd"], 2
        )

output = processed / "priority_orders.csv"
columns = list(candidates[0])
with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(selected)

assert spent <= budget + 0.01
assert all(row["past_unmet_units"] > 0 for row in selected)

plan = {
    "decision_date": cutoff.isoformat(),
    "training_dates": "2026-01-01 through 2026-02-14",
    "training_average_inventory_value_usd": round(
        training_inventory_value, 2
    ),
    "extra_purchase_cost_budget_usd": budget,
    "selected_orders": len(selected),
    "selected_units": sum(
        row["order_units"] for row in selected
    ),
    "estimated_purchase_cost_usd": spent,
    "selection_rule": (
        "Past unmet units per purchase dollar; "
        "all ranking and budget inputs known by Feb 14"
    ),
    "limit": (
        "Purchase cost is a planning cap. The replay must "
        "measure actual fill rate, stockouts, and inventory value."
    ),
}
(reports / "13_priority_order_plan.json").write_text(
    json.dumps(plan, indent=2), encoding="utf-8"
)
print(json.dumps(plan, indent=2))