from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import json

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"
cutoff = date(2026, 2, 14)
start = date(2026, 2, 15)
end = date(2026, 3, 1)

def load(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

products = {
    row["sku"]: row for row in load(raw / "products.csv")
}
orders = load(processed / "baseline_purchase_orders.csv")
baseline = load(processed / "baseline_daily.csv")

stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in baseline if row["date"] == cutoff.isoformat()
}
assert len(stock) == 1200

# Compare receipts reconstructed from POs with the saved baseline.
original_arrivals = defaultdict(int)
expedited_arrivals = defaultdict(int)
expedited_orders = 0
expedited_units = 0

for order in orders:
    placed = date.fromisoformat(order["order_date"])
    original = date.fromisoformat(
        order["actual_receipt_date"]
    )
    expected = date.fromisoformat(order["expected_date"])
    key = (order["store_id"], order["sku"])
    units = int(order["ordered_units"])

    if start <= original <= end:
        original_arrivals[(original, key)] += units

    new_arrival = original

    # On an overdue order, escalation is triggered only after
    # the promised day has passed without a receipt.
    if placed <= end and original > expected:
        escalation_day = max(expected, cutoff)
        possible_arrival = escalation_day + timedelta(days=1)
        if possible_arrival < original:
            new_arrival = possible_arrival
            if start <= new_arrival <= end:
                expedited_orders += 1
                expedited_units += units

    if start <= new_arrival <= end:
        expedited_arrivals[(new_arrival, key)] += units

test_rows = [
    row for row in baseline
    if start <= date.fromisoformat(row["date"]) <= end
]
test_rows.sort(key=lambda row: (
    row["date"], row["store_id"], row["sku"]
))
assert len(test_rows) == 18000

# Verify that reconstructed normal receipts match the baseline.
for row in test_rows:
    day = date.fromisoformat(row["date"])
    key = (row["store_id"], row["sku"])
    assert original_arrivals[(day, key)] == int(
        row["received_units"]
    ), f"Receipt mismatch on {day}, {key}"

outcomes = []
for row in test_rows:
    day = date.fromisoformat(row["date"])
    key = (row["store_id"], row["sku"])
    opening = stock[key]
    received = expedited_arrivals[(day, key)]
    requested = int(row["requested_units"])
    available = opening + received
    fulfilled = min(available, requested)
    unmet = requested - fulfilled
    stock[key] = available - fulfilled

    assert stock[key] >= 0
    assert stock[key] == opening + received - fulfilled

    outcomes.append({
        "date": row["date"],
        "store_id": key[0],
        "sku": key[1],
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "closing_units": stock[key],
    })

def measure(rows):
    requested = sum(int(row["requested_units"]) for row in rows)
    fulfilled = sum(int(row["fulfilled_units"]) for row in rows)
    inventory = sum(
        int(row["closing_units"])
        * float(products[row["sku"]]["unit_cost_usd"])
        for row in rows
    ) / 15
    return {
        "fill_rate_percent": round(
            100 * fulfilled / requested, 2
        ),
        "stockout_store_sku_days": sum(
            int(row["unmet_units"]) > 0 for row in rows
        ),
        "average_daily_inventory_value_usd": round(
            inventory, 2
        ),
    }

old = measure(test_rows)
new = measure(outcomes)
impact = {
    "test_window": "2026-02-15 through 2026-03-01",
    "orders_arriving_earlier": expedited_orders,
    "units_arriving_earlier": expedited_units,
    "estimated_expedite_cost_usd": 25 * expedited_orders,
    "baseline": old,
    "baseline_with_expedites": new,
    "change": {
        "fill_rate_percentage_points": round(
            new["fill_rate_percent"] - old["fill_rate_percent"],
            2
        ),
        "stockout_days_percent": round(
            100 * (new["stockout_store_sku_days"]
                   / old["stockout_store_sku_days"] - 1),
            2
        ),
        "inventory_value_percent": round(
            100 * (new["average_daily_inventory_value_usd"]
                   / old["average_daily_inventory_value_usd"] - 1),
            2
        ),
    },
    "assumptions": (
        "Every late order can be expedited to the day after its "
        "missed promise date for $25. Baseline purchase quantities "
        "and future PO schedule remain fixed."
    ),
}
(reports / "15_expedite_impact.json").write_text(
    json.dumps(impact, indent=2), encoding="utf-8"
)
print(json.dumps(impact, indent=2))