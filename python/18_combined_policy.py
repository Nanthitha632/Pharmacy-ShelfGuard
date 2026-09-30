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

products = {r["sku"]: r for r in load(raw / "products.csv")}
baseline = load(processed / "baseline_daily.csv")
purchase_orders = load(
    processed / "baseline_purchase_orders.csv"
)
priority_orders = load(processed / "priority_orders.csv")

stock = {
    (r["store_id"], r["sku"]): int(r["closing_units"])
    for r in baseline if r["date"] == cutoff.isoformat()
}
assert len(stock) == 1200

supplier_arrivals = defaultdict(int)
priority_arrivals = defaultdict(int)
expedited_po_ids = set()

# Preserve baseline PO quantities; move late receipts earlier
# only after their promise date has passed without delivery.
for po in purchase_orders:
    placed = date.fromisoformat(po["order_date"])
    expected = date.fromisoformat(po["expected_date"])
    original = date.fromisoformat(po["actual_receipt_date"])
    arrival = original

    if placed <= end and original > expected:
        escalation_day = max(expected, cutoff)
        next_day = escalation_day + timedelta(days=1)
        if next_day < original:
            arrival = next_day
            if start <= arrival <= end:
                expedited_po_ids.add(po["po_id"])

    if start <= arrival <= end:
        supplier_arrivals[
            (arrival.isoformat(), po["store_id"], po["sku"])
        ] += int(po["ordered_units"])

# Priority orders were selected using data through Feb 14 only.
for order in priority_orders:
    label = (
        f"{order['store_id']}|{order['sku']}|"
        f"{cutoff.isoformat()}"
    )
    number = int(
        hashlib.sha256(label.encode()).hexdigest()[:8], 16
    )
    lead_days = 2 + number % 4 + (
        2 if number % 7 == 0 else 0
    )
    arrival = cutoff + timedelta(days=lead_days)
    if start <= arrival <= end:
        priority_arrivals[
            (arrival.isoformat(),
             order["store_id"], order["sku"])
        ] += int(order["order_units"])

test_rows = [
    r for r in baseline
    if start <= date.fromisoformat(r["date"]) <= end
]
test_rows.sort(key=lambda r: (
    r["date"], r["store_id"], r["sku"]
))
assert len(test_rows) == 18000

daily = []

for original in test_rows:
    day = original["date"]
    store = original["store_id"]
    sku = original["sku"]
    key = (store, sku)
    event = (day, store, sku)

    opening = stock[key]
    supplier_units = supplier_arrivals[event]
    priority_units = priority_arrivals[event]
    requested = int(original["requested_units"])
    available = opening + supplier_units + priority_units
    fulfilled = min(available, requested)
    unmet = requested - fulfilled
    stock[key] = available - fulfilled

    assert stock[key] >= 0
    assert fulfilled + unmet == requested
    assert (
        opening + supplier_units + priority_units - fulfilled
        == stock[key]
    )

    daily.append({
        "date": day,
        "store_id": store,
        "sku": sku,
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "supplier_received_units": supplier_units,
        "priority_received_units": priority_units,
        "closing_units": stock[key],
    })

output = processed / "combined_policy_daily.csv"
with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=list(daily[0]))
    writer.writeheader()
    writer.writerows(daily)

def metrics(rows):
    requested = sum(int(r["requested_units"]) for r in rows)
    fulfilled = sum(int(r["fulfilled_units"]) for r in rows)
    value = sum(
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
            value, 2
        ),
    }

old = metrics(test_rows)
new = metrics(daily)
assert old["requested_units"] == new["requested_units"]

result = {
    "evaluation_window": "2026-02-15 through 2026-03-01",
    "priority_orders": len(priority_orders),
    "expedited_purchase_orders": len(expedited_po_ids),
    "estimated_expedite_cost_usd":
        25 * len(expedited_po_ids),
    "baseline": old,
    "combined_policy": new,
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
    "assumptions": (
        "Baseline future PO quantities remain fixed; an overdue "
        "order can arrive the next day for $25. Supplier ability "
        "to expedite and the fee are synthetic assumptions."
    ),
    "status": "Candidate policy; report measured results only",
}
(reports / "18_combined_policy.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
print(json.dumps(result, indent=2))