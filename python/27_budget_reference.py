"""Compare a new purchase proposal with known April purchasing activity."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import csv
import json
import math

root = Path(__file__).resolve().parents[1]
products_file = root / "data" / "raw" / "products.csv"
orders_file = root / "data" / "v2" / "processed" / "purchase_orders_120d.csv"
report_file = root / "reports" / "27_budget_reference.json"

def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))

cost_by_sku = {
    row["sku"]: Decimal(row["unit_cost_usd"])
    for row in read_csv(products_file)
}
assert len(cost_by_sku) == 30

first = date(2026, 4, 1)
days = [first + timedelta(days=i) for i in range(30)]
spend = {day: Decimal("0") for day in days}
lines = defaultdict(int)
seen_ids = set()

for po in read_csv(orders_file):
    placed = date.fromisoformat(po["order_date"])
    if placed not in spend:
        continue
    if po["po_id"] in seen_ids:
        raise ValueError(f"Duplicate PO: {po['po_id']}")
    seen_ids.add(po["po_id"])
    spend[placed] += (
        Decimal(po["ordered_units"]) * cost_by_sku[po["sku"]]
    )
    lines[placed] += 1

if not seen_ids:
    raise ValueError("No April purchase orders found")

daily_values = sorted(spend.values())
p90 = daily_values[math.ceil(0.90 * len(daily_values)) - 1]
proposal = Decimal("413578.26")

report = {
    "source": "Synthetic baseline POs placed April 1-30, 2026",
    "april_po_lines": len(seen_ids),
    "mean_daily_spend_usd": round(float(sum(daily_values) / 30), 2),
    "median_daily_spend_usd": round(float(daily_values[14] + daily_values[15]) / 2, 2),
    "p90_daily_spend_usd": round(float(p90), 2),
    "highest_daily_spend_usd": round(float(daily_values[-1]), 2),
    "may_1_proposed_spend_usd": float(proposal),
    "proposal_to_april_p90_ratio": (
        round(float(proposal / p90), 2) if p90 else None
    ),
    "interpretation": (
        "April's 90th percentile is a planning reference, not an "
        "approved budget or proof that the proposed orders improve service."
    ),
}
report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
print(f"Saved: {report_file}")