from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import json
import random

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
old_processed = root / "data" / "processed"
v2_raw = root / "data" / "v2" / "raw"
v2_out = root / "data" / "v2" / "processed"
reports = root / "reports"
v2_out.mkdir(parents=True, exist_ok=True)

def load(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

def save(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

products = {
    row["sku"]: row for row in load(raw / "products.csv")
}
old_demand = load(raw / "customer_demand.csv")
all_demand = load(
    v2_raw / "customer_demand_120d.csv"
)
old_daily = load(old_processed / "baseline_daily.csv")
purchase_orders = load(
    old_processed / "baseline_purchase_orders.csv"
)
goods_receipts = load(
    old_processed / "baseline_goods_receipts.csv"
)

assert len(old_demand) == len(old_daily) == 72000
assert len(all_demand) == 144000
assert all_demand[:72000] == old_demand

cutoff = date(2026, 3, 1)
stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in old_daily if row["date"] == cutoff.isoformat()
}
assert len(stock) == 1200

# Reconstruct the original random generator's state. Checking every
# historical PO proves that new supplier events continue the same model.
rng = random.Random(314159)
for order in purchase_orders:
    placed = date.fromisoformat(order["order_date"])
    normal = rng.randint(2, 5)
    delay = rng.randint(1, 3) if rng.random() < 0.15 else 0
    assert order["expected_date"] == (
        placed + timedelta(days=normal)
    ).isoformat()
    assert order["actual_receipt_date"] == (
        placed + timedelta(days=normal + delay)
    ).isoformat()

open_orders = defaultdict(int)
scheduled = defaultdict(list)
for order in purchase_orders:
    arrival = date.fromisoformat(
        order["actual_receipt_date"]
    )
    if arrival > cutoff:
        key = (order["store_id"], order["sku"])
        units = int(order["ordered_units"])
        scheduled[(arrival, key)].append(
            (order["po_id"], units)
        )
        open_orders[key] += units

new_daily = []

for row in all_demand[72000:]:
    day = date.fromisoformat(row["demand_date"])
    key = (row["store_id"], row["sku"])
    product = products[row["sku"]]
    base = int(product["base_daily_demand"])
    opening = stock[key]

    received = 0
    for po_id, units in scheduled.pop((day, key), []):
        received += units
        open_orders[key] -= units
        goods_receipts.append({
            "receipt_id": f"GR{len(goods_receipts) + 1:06d}",
            "po_id": po_id,
            "receipt_date": day.isoformat(),
            "store_id": key[0],
            "sku": key[1],
            "accepted_units": units,
        })
    stock[key] += received

    requested = int(row["requested_units"])
    fulfilled = min(requested, stock[key])
    unmet = requested - fulfilled
    stock[key] -= fulfilled

    position = stock[key] + open_orders[key]
    ordered = 0
    if position < 4 * base:
        pack = int(product["case_pack"])
        shortage = 9 * base - position
        ordered = ((shortage + pack - 1) // pack) * pack

        normal = rng.randint(2, 5)
        delay = (
            rng.randint(1, 3) if rng.random() < 0.15 else 0
        )
        arrival = day + timedelta(days=normal + delay)
        po_id = f"PO{len(purchase_orders) + 1:06d}"

        purchase_orders.append({
            "po_id": po_id,
            "order_date": day.isoformat(),
            "expected_date": (
                day + timedelta(days=normal)
            ).isoformat(),
            "actual_receipt_date": arrival.isoformat(),
            "store_id": key[0],
            "sku": key[1],
            "supplier_id": product["supplier_id"],
            "ordered_units": ordered,
        })
        scheduled[(arrival, key)].append(
            (po_id, ordered)
        )
        open_orders[key] += ordered

    assert stock[key] >= 0
    assert opening + received - fulfilled == stock[key]
    assert requested == fulfilled + unmet

    new_daily.append({
        "date": day.isoformat(),
        "store_id": key[0],
        "sku": key[1],
        "opening_units": opening,
        "received_units": received,
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "ordered_units": ordered,
        "closing_units": stock[key],
    })

daily = old_daily + new_daily
assert len(daily) == 144000
assert len({(r["date"], r["store_id"], r["sku"])
            for r in daily}) == 144000

save(
    v2_out / "baseline_daily_120d.csv",
    list(old_daily[0]), daily
)
save(
    v2_out / "purchase_orders_120d.csv",
    list(purchase_orders[0]), purchase_orders
)
save(
    v2_out / "goods_receipts_120d.csv",
    list(goods_receipts[0]), goods_receipts
)

summary = {
    "simulation": "ShelfGuard v2 continuous baseline",
    "demand_records": len(all_demand),
    "daily_inventory_records": len(daily),
    "purchase_orders": len(purchase_orders),
    "goods_receipts_through_april_30": len(goods_receipts),
    "validation": (
        "Passed: original PO sequence reproduced; "
        "inventory and demand balance; unique daily keys"
    ),
    "evaluation_rule": (
        "No April outcome metrics inspected before policy lock"
    ),
}
(reports / "20_v2_record_counts.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary, indent=2))