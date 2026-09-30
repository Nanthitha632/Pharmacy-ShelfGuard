from pathlib import Path
from collections import Counter
from datetime import date, timedelta
import csv
import json

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
report_path = root / "reports" / "02_demand_audit.json"

def read_csv(name):
    with (raw / name).open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

stores = read_csv("stores.csv")
products = read_csv("products.csv")
suppliers = read_csv("suppliers.csv")
demand = read_csv("customer_demand.csv")

store_ids = {row["store_id"] for row in stores}
skus = {row["sku"] for row in products}
supplier_ids = {row["supplier_id"] for row in suppliers}
dates = {
    (date(2026, 1, 1) + timedelta(days=i)).isoformat()
    for i in range(60)
}

keys = Counter(
    (row["demand_date"], row["store_id"], row["sku"])
    for row in demand
)
duplicate_keys = sum(count - 1 for count in keys.values() if count > 1)
unknown_stores = sum(row["store_id"] not in store_ids for row in demand)
unknown_products = sum(row["sku"] not in skus for row in demand)
invalid_dates = sum(row["demand_date"] not in dates for row in demand)

invalid_quantities = 0
units_by_store = Counter()
units_by_category = Counter()
category_by_sku = {row["sku"]: row["category"] for row in products}

for row in demand:
    try:
        units = int(row["requested_units"])
        if units < 0:
            invalid_quantities += 1
            continue
    except ValueError:
        invalid_quantities += 1
        continue

    units_by_store[row["store_id"]] += units
    if row["sku"] in category_by_sku:
        units_by_category[category_by_sku[row["sku"]]] += units

missing_combinations = len(dates) * len(store_ids) * len(skus) - len(keys)
invalid_product_suppliers = sum(
    row["supplier_id"] not in supplier_ids for row in products
)

checks = {
    "expected_demand_rows": 72000,
    "actual_demand_rows": len(demand),
    "duplicate_store_sku_date_rows": duplicate_keys,
    "missing_store_sku_date_combinations": missing_combinations,
    "unknown_store_rows": unknown_stores,
    "unknown_product_rows": unknown_products,
    "invalid_date_rows": invalid_dates,
    "invalid_quantity_rows": invalid_quantities,
    "products_with_unknown_supplier": invalid_product_suppliers,
}

passed = (
    len(demand) == 72000
    and all(value == 0 for key, value in checks.items()
            if key not in ("expected_demand_rows", "actual_demand_rows"))
)

report = {
    "status": "PASS" if passed else "FAIL",
    "checks": checks,
    "total_requested_units": sum(units_by_store.values()),
    "requested_units_by_category": dict(sorted(units_by_category.items())),
    "highest_demand_store": max(units_by_store, key=units_by_store.get),
    "highest_demand_store_units": max(units_by_store.values()),
    "interpretation": (
        "Customer requests are complete and linked to valid stores and SKUs."
        if passed else
        "Resolve failed checks before forecasting or purchasing."
    ),
}

report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
print(f"\nSaved audit: {report_path.relative_to(root)}")