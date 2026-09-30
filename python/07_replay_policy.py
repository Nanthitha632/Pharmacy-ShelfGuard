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
test_start = cutoff + timedelta(days=1)
test_end = date(2026, 3, 1)

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

products = {
    row["sku"]: row for row in read_csv(raw / "products.csv")
}
baseline = read_csv(processed / "baseline_daily.csv")
all_orders = read_csv(processed / "baseline_purchase_orders.csv")
demand = read_csv(raw / "customer_demand.csv")
recommendations = read_csv(
    processed / "order_recommendations.csv"
)

def mean(values):
    return sum(values) / len(values)

def std(values):
    average = mean(values)
    return sqrt(
        sum((value - average) ** 2 for value in values) / len(values)
    )

# Starting position: precisely the baseline's closing stock on Feb 14.
stock = {}
for row in baseline:
    if row["date"] == cutoff.isoformat():
        stock[(row["store_id"], row["sku"])] = int(
            row["closing_units"]
        )
assert len(stock) == 1200

# Orders placed by Feb 14 are already committed in both scenarios.
arrivals = defaultdict(list)
outstanding = defaultdict(int)
for order in all_orders:
    placed = date.fromisoformat(order["order_date"])
    arrival = date.fromisoformat(order["actual_receipt_date"])
    if placed <= cutoff and arrival > cutoff:
        key = (order["store_id"], order["sku"])
        units = int(order["ordered_units"])
        arrivals[(arrival, key)].append(units)
        outstanding[key] += units

# Demand history starts with the 14 days available before the test.
history = defaultdict(lambda: deque(maxlen=14))
for row in demand:
    day = date.fromisoformat(row["demand_date"])
    if day <= cutoff:
        history[(row["store_id"], row["sku"])].append(
            int(row["requested_units"])
        )

# Supplier estimates from the saved Feb 14 recommendation.
supplier_leads = {}
for row in recommendations:
    supplier_leads[row["supplier_id"]] = (
        float(row["mean_supplier_lead_days"]),
        float(row["supplier_lead_std_days"]),
    )

# A stable synthetic delivery delay: rerunning produces the same result.
def delivery_days(store_id, sku, order_day):
    label = f"{store_id}|{sku}|{order_day.isoformat()}"
    number = int(hashlib.sha256(label.encode()).hexdigest()[:8], 16)
    return 2 + number % 4 + (2 if number % 7 == 0 else 0)

future_demand = [
    row for row in demand
    if test_start <= date.fromisoformat(row["demand_date"]) <= test_end
]
future_demand.sort(key=lambda r: (
    r["demand_date"], r["store_id"], r["sku"]
))
assert len(future_demand) == 18000

improved = []
new_order_count = 0

for row in future_demand:
    day = date.fromisoformat(row["demand_date"])
    key = (row["store_id"], row["sku"])
    product = products[row["sku"]]
    opening = stock[key]

    received = sum(arrivals.pop((day, key), []))
    outstanding[key] -= received
    stock[key] += received

    requested = int(row["requested_units"])
    fulfilled = min(stock[key], requested)
    stock[key] -= fulfilled
    unmet = requested - fulfilled

    # Today's request is known at the end-of-day ordering review.
    history[key].append(requested)
    recent = list(history[key])
    forecast = mean(recent)
    demand_std = std(recent)
    lead_mean, lead_std = supplier_leads[product["supplier_id"]]

    safety = ceil(1.65 * sqrt(
        lead_mean * demand_std ** 2
        + forecast ** 2 * lead_std ** 2
    ))
    target = ceil(forecast * (lead_mean + 1) + safety)
    position = stock[key] + outstanding[key]
    needed = max(0, target - position)
    pack = int(product["case_pack"])
    order_units = ceil(needed / pack) * pack

    if order_units:
        arrival_day = day + timedelta(
            days=delivery_days(key[0], key[1], day)
        )
        arrivals[(arrival_day, key)].append(order_units)
        outstanding[key] += order_units
        new_order_count += 1

    assert opening + received - fulfilled == stock[key]
    assert fulfilled + unmet == requested
    assert stock[key] >= 0

    improved.append({
        "date": day.isoformat(),
        "store_id": key[0],
        "sku": key[1],
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "received_units": received,
        "ordered_units": order_units,
        "closing_units": stock[key],
    })

with (processed / "improved_daily.csv").open(
    "w", newline="", encoding="utf-8"
) as file:
    writer = csv.DictWriter(file, fieldnames=list(improved[0]))
    writer.writeheader()
    writer.writerows(improved)

baseline_test = [
    row for row in baseline
    if test_start <= date.fromisoformat(row["date"]) <= test_end
]
assert len(baseline_test) == len(improved)

def metrics(rows):
    requested = sum(int(r["requested_units"]) for r in rows)
    fulfilled = sum(int(r["fulfilled_units"]) for r in rows)
    stockout_days = sum(int(r["unmet_units"]) > 0 for r in rows)
    inventory_value = sum(
        int(r["closing_units"])
        * float(products[r["sku"]]["unit_cost_usd"])
        for r in rows
    ) / 15
    return {
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "fill_rate_percent": round(100 * fulfilled / requested, 2),
        "stockout_store_sku_days": stockout_days,
        "average_daily_inventory_value_usd": round(
            inventory_value, 2
        ),
    }

old = metrics(baseline_test)
new = metrics(improved)
assert old["requested_units"] == new["requested_units"]

comparison = {
    "window": "2026-02-15 through 2026-03-01",
    "baseline": old,
    "improved_policy_pilot": new,
    "new_policy_orders_in_test_window": new_order_count,
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
    "validation": (
        "Same test demand and Feb 14 stock; no future demand used "
        "for an order decision; inventory balances passed"
    ),
    "limitation": (
        "Pilot delivery delays for new orders are deterministic "
        "synthetic draws; finalize a common supplier-event model "
        "before treating this as the portfolio evaluation."
    ),
}
(reports / "07_policy_comparison.json").write_text(
    json.dumps(comparison, indent=2), encoding="utf-8"
)
print(json.dumps(comparison, indent=2))