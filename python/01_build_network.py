from pathlib import Path
from datetime import date, timedelta
import csv
import json
import random

# A fixed seed makes every run produce the same synthetic network.
rng = random.Random(20260924)
root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
reports = root / "reports"
raw.mkdir(parents=True, exist_ok=True)
reports.mkdir(parents=True, exist_ok=True)

def save_csv(filename, columns, rows):
    with (raw / filename).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

# Four regions let us compare demand and supply across the network.
regions = ["Dallas", "Fort Worth", "Denton", "Arlington"]
stores = [
    {"store_id": f"ST{i:03d}", "region": regions[(i - 1) % 4]}
    for i in range(1, 41)
]

suppliers = [
    {"supplier_id": f"SUP{i:02d}", "supplier_name": f"Synthetic Supplier {i}"}
    for i in range(1, 5)
]

categories = ["Allergy", "Cold and Flu", "Pain Relief", "Digestive", "Wellness"]
products = []
for i in range(1, 31):
    products.append({
        "sku": f"MED{i:03d}",
        "product_name": f"Synthetic Medicine {i:03d}",
        "category": categories[(i - 1) % 5],
        "supplier_id": suppliers[(i - 1) % 4]["supplier_id"],
        "unit_cost_usd": f"{rng.uniform(3.00, 24.00):.2f}",
        "case_pack": rng.choice([6, 12, 24]),
        "base_daily_demand": rng.randint(3, 12),
    })

# Each row represents what customers requested, including demand
# that a store might later be unable to fulfill.
start = date(2026, 1, 1)
demand = []
for day_number in range(60):
    current = start + timedelta(days=day_number)
    weekend = current.weekday() >= 5

    for store in stores:
        for product in products:
            store_factor = 0.75 + (int(store["store_id"][-3:]) % 7) * 0.09
            seasonal_factor = 1.30 if (
                product["category"] == "Cold and Flu" and day_number < 25
            ) else 1.0
            weekend_factor = 1.12 if weekend else 1.0
            typical = (
                product["base_daily_demand"]
                * store_factor
                * seasonal_factor
                * weekend_factor
            )

            # Occasional local spikes create realistic planning exceptions.
            spike = 1.8 if rng.random() < 0.015 else 1.0
            requested = max(0, round(rng.gauss(typical * spike, 2.2)))

            demand.append({
                "demand_date": current.isoformat(),
                "store_id": store["store_id"],
                "sku": product["sku"],
                "requested_units": requested,
            })

save_csv("stores.csv", ["store_id", "region"], stores)
save_csv("suppliers.csv", ["supplier_id", "supplier_name"], suppliers)
save_csv(
    "products.csv",
    ["sku", "product_name", "category", "supplier_id",
     "unit_cost_usd", "case_pack", "base_daily_demand"],
    products,
)
save_csv(
    "customer_demand.csv",
    ["demand_date", "store_id", "sku", "requested_units"],
    demand,
)

assert len(demand) == 40 * 30 * 60
assert len({(r["demand_date"], r["store_id"], r["sku"]) for r in demand}) == len(demand)
assert all(r["requested_units"] >= 0 for r in demand)

summary = {
    "simulation": "Pharmacy ShelfGuard — synthetic data",
    "random_seed": 20260924,
    "stores": len(stores),
    "products": len(products),
    "suppliers": len(suppliers),
    "days": 60,
    "customer_demand_records": len(demand),
    "total_requested_units": sum(r["requested_units"] for r in demand),
    "validation": "Passed: unique store-SKU-date rows and nonnegative requests",
}
(reports / "01_network_summary.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)

print(json.dumps(summary, indent=2))
print("\nSaved source records in data/raw/ and summary in reports/.")