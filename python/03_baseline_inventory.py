from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import json
import random

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"
processed.mkdir(exist_ok=True)
reports.mkdir(exist_ok=True)

audit = json.loads((reports / "02_demand_audit.json").read_text(encoding="utf-8"))
if audit["status"] != "PASS":
    raise SystemExit("Demand audit failed. Fix it before simulating inventory.")

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

def save_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

products = {
    row["sku"]: row for row in read_csv(raw / "products.csv")
}
demand = read_csv(raw / "customer_demand.csv")
demand.sort(key=lambda row: (
    row["demand_date"], row["store_id"], row["sku"]
))

rng = random.Random(314159)
stock = {}
open_orders = defaultdict(int)
scheduled_receipts = defaultdict(list)
daily_results = []
purchase_orders = []
goods_receipts = []

for row in demand:
    day = date.fromisoformat(row["demand_date"])
    store_id, sku = row["store_id"], row["sku"]
    key = (store_id, sku)
    product = products[sku]
    base = int(product["base_daily_demand"])

    # Starting stock is approximately five days of typical demand.
    if key not in stock:
        stock[key] = 5 * base

    opening = stock[key]
    received = 0

    # Receive orders arriving today before serving customers.
    for order in scheduled_receipts.pop((day, key), []):
        quantity = order["ordered_units"]
        received += quantity
        open_orders[key] -= quantity
        goods_receipts.append({
            "receipt_id": f"GR{len(goods_receipts) + 1:06d}",
            "po_id": order["po_id"],
            "receipt_date": day.isoformat(),
            "store_id": store_id,
            "sku": sku,
            "accepted_units": quantity,
        })
    stock[key] += received

    requested = int(row["requested_units"])
    fulfilled = min(requested, stock[key])
    unmet = requested - fulfilled
    stock[key] -= fulfilled

    # Baseline rule: order when stock plus incoming units is below
    # four days of typical demand; order toward nine days.
    inventory_position = stock[key] + open_orders[key]
    ordered = 0
    if inventory_position < 4 * base:
        case_pack = int(product["case_pack"])
        shortage = 9 * base - inventory_position
        ordered = ((shortage + case_pack - 1) // case_pack) * case_pack

        normal_lead_days = rng.randint(2, 5)
        delay_days = rng.randint(1, 3) if rng.random() < 0.15 else 0
        arrival = day + timedelta(days=normal_lead_days + delay_days)
        po_id = f"PO{len(purchase_orders) + 1:06d}"

        purchase_orders.append({
            "po_id": po_id,
            "order_date": day.isoformat(),
            "expected_date": (day + timedelta(days=normal_lead_days)).isoformat(),
            "actual_receipt_date": arrival.isoformat(),
            "store_id": store_id,
            "sku": sku,
            "supplier_id": product["supplier_id"],
            "ordered_units": ordered,
        })
        scheduled_receipts[(arrival, key)].append({
            "po_id": po_id,
            "ordered_units": ordered,
        })
        open_orders[key] += ordered

    assert stock[key] >= 0
    assert opening + received - fulfilled == stock[key]
    assert fulfilled + unmet == requested

    daily_results.append({
        "date": day.isoformat(),
        "store_id": store_id,
        "sku": sku,
        "opening_units": opening,
        "received_units": received,
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "ordered_units": ordered,
        "closing_units": stock[key],
    })

save_csv(
    processed / "baseline_daily.csv",
    ["date", "store_id", "sku", "opening_units", "received_units",
     "requested_units", "fulfilled_units", "unmet_units",
     "ordered_units", "closing_units"],
    daily_results,
)
save_csv(
    processed / "baseline_purchase_orders.csv",
    ["po_id", "order_date", "expected_date", "actual_receipt_date",
     "store_id", "sku", "supplier_id", "ordered_units"],
    purchase_orders,
)
save_csv(
    processed / "baseline_goods_receipts.csv",
    ["receipt_id", "po_id", "receipt_date", "store_id", "sku",
     "accepted_units"],
    goods_receipts,
)

requested_total = sum(r["requested_units"] for r in daily_results)
fulfilled_total = sum(r["fulfilled_units"] for r in daily_results)
stockout_days = sum(r["unmet_units"] > 0 for r in daily_results)
inventory_value = sum(
    r["closing_units"] * float(products[r["sku"]]["unit_cost_usd"])
    for r in daily_results
) / 60

summary = {
    "policy": "Baseline: reorder below 4 days; target 9 days",
    "daily_store_sku_records": len(daily_results),
    "purchase_orders": len(purchase_orders),
    "goods_receipts_within_60_days": len(goods_receipts),
    "requested_units": requested_total,
    "fulfilled_units": fulfilled_total,
    "fill_rate_percent": round(100 * fulfilled_total / requested_total, 2),
    "stockout_store_sku_days": stockout_days,
    "average_daily_inventory_value_usd": round(inventory_value, 2),
    "validation": "Passed: no negative stock; inventory and demand balance",
}
(reports / "03_baseline_metrics.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary, indent=2))
print("\nSaved baseline records in data/processed/ and metrics in reports/.")