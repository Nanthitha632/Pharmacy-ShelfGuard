from pathlib import Path
from collections import defaultdict
from datetime import date
from math import ceil, sqrt
import csv
import json

def mean(values):
    return sum(values) / len(values)

def pstdev(values):
    average = mean(values)
    return sqrt(
        sum((value - average) ** 2 for value in values) / len(values)
    )

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"
as_of = date(2026, 2, 14)

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

products = {
    row["sku"]: row for row in read_csv(raw / "products.csv")
}

# Learn demand only from dates known on the decision day.
history = defaultdict(list)
for row in read_csv(raw / "customer_demand.csv"):
    if date.fromisoformat(row["demand_date"]) <= as_of:
        history[(row["store_id"], row["sku"])].append(
            int(row["requested_units"])
        )

# Find the stock actually on hand at the end of February 14.
closing_stock = {}
for row in read_csv(processed / "baseline_daily.csv"):
    if row["date"] == as_of.isoformat():
        closing_stock[(row["store_id"], row["sku"])] = int(
            row["closing_units"]
        )

orders = read_csv(processed / "baseline_purchase_orders.csv")
receipts = {
    row["po_id"]: row
    for row in read_csv(processed / "baseline_goods_receipts.csv")
}

lead_days = defaultdict(list)
on_order = defaultdict(int)

for order in orders:
    placed = date.fromisoformat(order["order_date"])
    if placed > as_of:
        continue

    receipt = receipts.get(order["po_id"])
    received_by_cutoff = (
        receipt is not None
        and date.fromisoformat(receipt["receipt_date"]) <= as_of
    )

    if received_by_cutoff:
        actual = date.fromisoformat(receipt["receipt_date"])
        lead_days[order["supplier_id"]].append(
            (actual - placed).days
        )
    else:
        on_order[(order["store_id"], order["sku"])] += int(
            order["ordered_units"]
        )

recommendations = []

for (store_id, sku), values in sorted(history.items()):
    product = products[sku]
    supplier_id = product["supplier_id"]
    observed_leads = lead_days[supplier_id]

    if not observed_leads:
        raise SystemExit(
            f"No completed supplier deliveries for {supplier_id}"
        )

    recent_demand = values[-14:]
    daily_forecast = mean(recent_demand)
    demand_std = pstdev(recent_demand)
    lead_mean = mean(observed_leads)
    lead_std = pstdev(observed_leads)

    # Buffer for uncertain customer demand and supplier delivery time.
    safety_stock = ceil(
        1.65 * sqrt(
            lead_mean * demand_std ** 2
            + daily_forecast ** 2 * lead_std ** 2
        )
    )

    # Cover lead time plus one day until the next stock review.
    target_stock = ceil(
        daily_forecast * (lead_mean + 1) + safety_stock
    )

    stock_on_hand = closing_stock[(store_id, sku)]
    already_ordered = on_order[(store_id, sku)]
    inventory_position = stock_on_hand + already_ordered

    units_needed = max(0, target_stock - inventory_position)
    case_pack = int(product["case_pack"])
    recommended_units = (
        ceil(units_needed / case_pack) * case_pack
    )

    recommendations.append({
        "as_of_date": as_of.isoformat(),
        "store_id": store_id,
        "sku": sku,
        "supplier_id": supplier_id,
        "daily_forecast_units": round(daily_forecast, 2),
        "mean_supplier_lead_days": round(lead_mean, 2),
        "supplier_lead_std_days": round(lead_std, 2),
        "safety_stock_units": safety_stock,
        "target_stock_units": target_stock,
        "closing_stock_units": stock_on_hand,
        "already_on_order_units": already_ordered,
        "recommended_order_units": recommended_units,
    })

assert len(recommendations) == 1200
assert all(
    row["recommended_order_units"] >= 0
    for row in recommendations
)

output = processed / "order_recommendations.csv"
with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(
        file, fieldnames=list(recommendations[0])
    )
    writer.writeheader()
    writer.writerows(recommendations)

summary = {
    "decision_date": as_of.isoformat(),
    "store_medicine_decisions": len(recommendations),
    "recommended_orders": sum(
        row["recommended_order_units"] > 0
        for row in recommendations
    ),
    "total_recommended_units": sum(
        row["recommended_order_units"]
        for row in recommendations
    ),
    "method": (
        "Recent demand forecast + observed supplier lead time "
        "+ safety stock; order in full case packs"
    ),
    "important_limit": (
        "This is a one-day recommendation, not a tested "
        "15-day policy."
    ),
}

(reports / "06_recommendation_summary.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)

print(json.dumps(summary, indent=2))
print("\nSaved recommendations in data/processed/.")