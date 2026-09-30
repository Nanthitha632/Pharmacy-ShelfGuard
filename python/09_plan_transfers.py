from pathlib import Path
from collections import defaultdict
import csv
import json

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

regions = {
    row["store_id"]: row["region"]
    for row in read_csv(raw / "stores.csv")
}
products = {
    row["sku"]: row
    for row in read_csv(raw / "products.csv")
}
recommendations = read_csv(
    processed / "order_recommendations.csv"
)

# Keep a working copy so one donor's stock cannot be promised twice.
stock = {
    (row["store_id"], row["sku"]): int(row["closing_stock_units"])
    for row in recommendations
}
forecast = {
    (row["store_id"], row["sku"]): float(
        row["daily_forecast_units"]
    )
    for row in recommendations
}

by_region_sku = defaultdict(list)
for row in recommendations:
    key = (regions[row["store_id"]], row["sku"])
    by_region_sku[key].append(row["store_id"])

proposals = []

for (region, sku), store_ids in sorted(by_region_sku.items()):
    # Attend to the lowest stock coverage first.
    receivers = sorted(
        store_ids,
        key=lambda store: stock[(store, sku)]
        / max(forecast[(store, sku)], 0.01)
    )

    for receiver in receivers:
        receiver_key = (receiver, sku)
        daily_need = forecast[receiver_key]
        if daily_need <= 0:
            continue

        # Lift the receiving store toward 3 days of stock.
        needed = max(
            0,
            round(3 * daily_need) - stock[receiver_key]
        )
        if stock[receiver_key] >= 2 * daily_need or needed == 0:
            continue

        donors = sorted(
            (store for store in store_ids if store != receiver),
            key=lambda store: stock[(store, sku)]
            / max(forecast[(store, sku)], 0.01),
            reverse=True,
        )

        for donor in donors:
            donor_key = (donor, sku)
            keep = round(6 * forecast[donor_key])
            spare = max(0, stock[donor_key] - keep)
            moved = min(needed, spare)
            if moved == 0:
                continue

            stock[donor_key] -= moved
            stock[receiver_key] += moved
            needed -= moved

            proposals.append({
                "decision_date": "2026-02-14",
                "earliest_arrival_date": "2026-02-15",
                "region": region,
                "sku": sku,
                "from_store": donor,
                "to_store": receiver,
                "transfer_units": moved,
                "unit_cost_usd": products[sku]["unit_cost_usd"],
                "status": "PROPOSED",
            })
            if needed == 0:
                break

output = processed / "transfer_proposals.csv"
columns = [
    "decision_date", "earliest_arrival_date", "region", "sku",
    "from_store", "to_store", "transfer_units",
    "unit_cost_usd", "status"
]
with output.open("w", newline="", encoding="utf-8") as file:
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(proposals)

assert all(row["transfer_units"] > 0 for row in proposals)

summary = {
    "decision_date": "2026-02-14",
    "proposed_transfers": len(proposals),
    "units_reallocated": sum(
        row["transfer_units"] for row in proposals
    ),
    "stock_purchase_units_added": 0,
    "assumptions": (
        "Same-region, same-SKU transfers arrive next day; donor "
        "retains 6 forecast days; proposal needs transport approval"
    ),
    "limit": (
        "This is a transfer plan, not a measured service improvement."
    ),
}
(reports / "09_transfer_summary.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)
print(json.dumps(summary, indent=2))
print("\nSaved individual proposals in data/processed/.")