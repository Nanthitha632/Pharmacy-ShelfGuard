from pathlib import Path
from datetime import date, timedelta
import csv
import random

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
out = root / "data" / "v2" / "raw"
out.mkdir(parents=True, exist_ok=True)

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

stores = read_csv(raw / "stores.csv")
products = read_csv(raw / "products.csv")
existing = read_csv(raw / "customer_demand.csv")
assert len(existing) == 72000

rng = random.Random(20260925)
extended = []

# The original 60 days end on March 1, 2026.
for day_number in range(60, 120):
    current = date(2026, 1, 1) + timedelta(days=day_number)
    weekend_factor = 1.12 if current.weekday() >= 5 else 1.0

    for store in stores:
        store_number = int(store["store_id"][-3:])
        store_factor = 0.75 + (store_number % 7) * 0.09

        for product in products:
            typical = (
                int(product["base_daily_demand"])
                * store_factor
                * weekend_factor
            )
            spike = 1.8 if rng.random() < 0.015 else 1.0
            requested = max(
                0, round(rng.gauss(typical * spike, 2.2))
            )

            extended.append({
                "demand_date": current.isoformat(),
                "store_id": store["store_id"],
                "sku": product["sku"],
                "requested_units": requested,
            })

assert len(extended) == 72000
all_rows = existing + extended
assert len(all_rows) == 144000
assert len({
    (r["demand_date"], r["store_id"], r["sku"])
    for r in all_rows
}) == 144000

output = out / "customer_demand_120d.csv"
columns = [
    "demand_date", "store_id", "sku", "requested_units"
]
with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(all_rows)

manifest = """# ShelfGuard v2 — evaluation dates

- **Development:** January 1–March 1, 2026 (60 days).
  Existing experiments belong here.
- **Policy selection:** March 2–March 31, 2026 (30 days).
  Choose and lock one policy using this window.
- **Final evaluation:** April 1–April 30, 2026 (30 days).
  Evaluate the locked policy once; do not tune using these outcomes.

The data is synthetic. The existing 60-day files and PostgreSQL
database remain the documented pilot. This v2 demand file contains
144,000 store–medicine–day requests. Purchases, receipts, and
inventory for the added days still need to be simulated before
service metrics can be calculated.
"""
(root / "docs" / "evaluation_split_v2.md").write_text(
    manifest, encoding="utf-8"
)

print("Created v2 demand: 144,000 rows across 120 days.")
print("Development: Jan 1–Mar 1 | Selection: Mar 2–31")
print("Untouched final evaluation: Apr 1–30")
print("Saved: data/v2/raw/customer_demand_120d.csv")
print("Saved: docs/evaluation_split_v2.md")