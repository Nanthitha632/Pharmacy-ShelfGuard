from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import hashlib
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
baseline = load(processed / "baseline_daily.csv")
priority_orders = load(processed / "priority_orders.csv")
plan = json.loads(
    (reports / "13_priority_order_plan.json").read_text(
        encoding="utf-8"
    )
)

stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in baseline if row["date"] == cutoff.isoformat()
}
assert len(stock) == 1200
assert len(priority_orders) == plan["selected_orders"]

def delivery_days(store_id, sku):
    label = f"{store_id}|{sku}|{cutoff.isoformat()}"
    number = int(
        hashlib.sha256(label.encode()).hexdigest()[:8], 16
    )
    return 2 + number % 4 + (2 if number % 7 == 0 else 0)

extra_arrivals = defaultdict(int)
for order in priority_orders:
    arrival = cutoff + timedelta(days=delivery_days(
        order["store_id"], order["sku"]
    ))
    extra_arrivals[
        (arrival.isoformat(), order["store_id"], order["sku"])
    ] += int(order["order_units"])

test_rows = [
    row for row in baseline
    if start <= date.fromisoformat(row["date"]) <= end
]
test_rows.sort(key=lambda row: (
    row["date"], row["store_id"], row["sku"]
))
assert len(test_rows) == 18000

results = []
for row in test_rows:
    key = (row["store_id"], row["sku"])
    arrival_key = (row["date"], key[0], key[1])
    extra = extra_arrivals[arrival_key]
    opening = stock[key]

    # Baseline supplier receipts stay fixed in this experiment.
    available = opening + int(row["received_units"]) + extra
    requested = int(row["requested_units"])
    fulfilled = min(available, requested)
    unmet = requested - fulfilled
    stock[key] = available - fulfilled

    assert stock[key] >= 0
    assert opening + int(row["received_units"]) + extra \
        - fulfilled == stock[key]

    results.append({
        "date": row["date"],
        "store_id": key[0],
        "sku": key[1],
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "baseline_supplier_received_units": int(
            row["received_units"]
        ),
        "priority_order_received_units": extra,
        "closing_units": stock[key],
    })

with (processed / "priority_replay_daily.csv").open(
    "w", newline="", encoding="utf-8"
) as file:
    writer = csv.DictWriter(
        file, fieldnames=list(results[0])
    )
    writer.writeheader()
    writer.writerows(results)

def measure(rows):
    requested = sum(int(r["requested_units"]) for r in rows)
    fulfilled = sum(int(r["fulfilled_units"]) for r in rows)
    inventory = sum(
        int(r["closing_units"])
        * float(products[r["sku"]]["unit_cost_usd"])
        for r in rows
    ) / 15
    return {
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "fill_rate_percent": round(
            100 * fulfilled / requested, 2
        ),
        "stockout_store_sku_days": sum(
            int(r["unmet_units"]) > 0 for r in rows
        ),
        "average_daily_inventory_value_usd": round(
            inventory, 2
        ),
    }

old = measure(test_rows)
new = measure(results)
assert old["requested_units"] == new["requested_units"]

impact = {
    "window": "2026-02-15 through 2026-03-01",
    "selected_extra_orders": len(priority_orders),
    "extra_units_received_within_window": sum(
        r["priority_order_received_units"] for r in results
    ),
    "baseline": old,
    "baseline_plus_priority_orders": new,
    "change": {
        "fill_rate_percentage_points": round(
            new["fill_rate_percent"]
            - old["fill_rate_percent"], 2
        ),
        "stockout_days_percent": round(
            100 * (new["stockout_store_sku_days"]
                   / old["stockout_store_sku_days"] - 1), 2
        ),
        "inventory_value_percent": round(
            100 * (new["average_daily_inventory_value_usd"]
                   / old["average_daily_inventory_value_usd"] - 1), 2
        ),
    },
    "limit": (
        "Baseline future orders and receipts remain fixed. "
        "This measures incremental priority purchases, not a "
        "complete replacement of the baseline ordering policy."
    ),
}
(reports / "14_priority_impact.json").write_text(
    json.dumps(impact, indent=2), encoding="utf-8"
)
print(json.dumps(impact, indent=2))