from pathlib import Path
from collections import defaultdict, deque
from datetime import date, timedelta
from math import ceil, sqrt
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

def average(values):
    return sum(values) / len(values)

def deviation(values):
    center = average(values)
    return sqrt(sum((x - center) ** 2 for x in values) / len(values))

def lead_days(store_id, sku, order_day):
    text = f"{store_id}|{sku}|{order_day.isoformat()}"
    value = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return 2 + value % 4 + (2 if value % 7 == 0 else 0)

products = {
    row["sku"]: row for row in load(raw / "products.csv")
}
demand = load(raw / "customer_demand.csv")
baseline = load(processed / "baseline_daily.csv")
orders = load(processed / "baseline_purchase_orders.csv")
recommendations = load(processed / "order_recommendations.csv")

stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in baseline if row["date"] == cutoff.isoformat()
}
assert len(stock) == 1200

arrivals = defaultdict(list)
outstanding = defaultdict(int)

def schedule(arrival_day, key, units):
    arrivals[(arrival_day, key)].append(units)
    outstanding[key] += units

# Both policies inherit orders already placed before the decision date.
for order in orders:
    placed = date.fromisoformat(order["order_date"])
    arrives = date.fromisoformat(order["actual_receipt_date"])
    if placed <= cutoff < arrives:
        schedule(
            arrives,
            (order["store_id"], order["sku"]),
            int(order["ordered_units"]),
        )

supplier_estimates = {}
initial_orders = 0

# This was missing from the previous pilot: place Step 6's orders.
for row in recommendations:
    supplier_estimates[row["supplier_id"]] = (
        float(row["mean_supplier_lead_days"]),
        float(row["supplier_lead_std_days"]),
    )
    units = int(row["recommended_order_units"])
    if units:
        key = (row["store_id"], row["sku"])
        schedule(
            cutoff + timedelta(days=lead_days(*key, cutoff)),
            key,
            units,
        )
        initial_orders += 1

assert initial_orders == 723

history = defaultdict(lambda: deque(maxlen=14))
for row in demand:
    if date.fromisoformat(row["demand_date"]) <= cutoff:
        history[(row["store_id"], row["sku"])].append(
            int(row["requested_units"])
        )

test_demand = [
    row for row in demand
    if start <= date.fromisoformat(row["demand_date"]) <= end
]
test_demand.sort(key=lambda row: (
    row["demand_date"], row["store_id"], row["sku"]
))
assert len(test_demand) == 18000

results = []
daily_new_orders = 0

for row in test_demand:
    day = date.fromisoformat(row["demand_date"])
    key = (row["store_id"], row["sku"])
    product = products[row["sku"]]
    opening = stock[key]

    received = sum(arrivals.pop((day, key), []))
    outstanding[key] -= received
    stock[key] += received

    requested = int(row["requested_units"])
    fulfilled = min(requested, stock[key])
    stock[key] -= fulfilled
    unmet = requested - fulfilled

    # Update with today's request only after today's customers are served.
    history[key].append(requested)
    recent = list(history[key])
    forecast = average(recent)
    demand_std = deviation(recent)
    supplier_mean, supplier_std = supplier_estimates[
        product["supplier_id"]
    ]

    safety = ceil(1.65 * sqrt(
        supplier_mean * demand_std ** 2
        + forecast ** 2 * supplier_std ** 2
    ))
    target = ceil(forecast * (supplier_mean + 1) + safety)
    needed = max(0, target - stock[key] - outstanding[key])
    pack = int(product["case_pack"])
    order_units = ceil(needed / pack) * pack

    if order_units:
        schedule(
            day + timedelta(days=lead_days(*key, day)),
            key,
            order_units,
        )
        daily_new_orders += 1

    assert stock[key] == opening + received - fulfilled
    assert requested == fulfilled + unmet

    results.append({
        "date": day.isoformat(),
        "store_id": key[0],
        "sku": key[1],
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "closing_units": stock[key],
    })

baseline_test = [
    row for row in baseline
    if start <= date.fromisoformat(row["date"]) <= end
]
assert len(baseline_test) == len(results)

def calculate(rows):
    requested = sum(int(r["requested_units"]) for r in rows)
    fulfilled = sum(int(r["fulfilled_units"]) for r in rows)
    value = sum(
        int(r["closing_units"])
        * float(products[r["sku"]]["unit_cost_usd"])
        for r in rows
    ) / 15
    return {
        "fill_rate_percent": round(100 * fulfilled / requested, 2),
        "stockout_store_sku_days": sum(
            int(r["unmet_units"]) > 0 for r in rows
        ),
        "average_daily_inventory_value_usd": round(value, 2),
    }

old = calculate(baseline_test)
new = calculate(results)
report = {
    "window": "2026-02-15 through 2026-03-01",
    "initial_recommendations_actually_placed": initial_orders,
    "additional_daily_orders": daily_new_orders,
    "baseline": old,
    "corrected_policy_pilot": new,
    "change": {
        "fill_rate_percentage_points": round(
            new["fill_rate_percent"] - old["fill_rate_percent"], 2
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
    "status": (
        "Corrected pilot; not the final calibrated portfolio result"
    ),
}
(reports / "08_corrected_pilot.json").write_text(
    json.dumps(report, indent=2), encoding="utf-8"
)
print(json.dumps(report, indent=2))