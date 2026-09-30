from pathlib import Path
from collections import defaultdict
from datetime import date
import csv
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

baseline = load(processed / "baseline_daily.csv")
transfers = load(processed / "transfer_proposals.csv")
products = {
    row["sku"]: row for row in load(raw / "products.csv")
}

# Start from exactly the same closing stock as the baseline.
stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in baseline if row["date"] == cutoff.isoformat()
}
assert len(stock) == 1200

incoming = defaultdict(int)
donor_keys = set()
total_moved = 0

for transfer in transfers:
    units = int(transfer["transfer_units"])
    donor = (transfer["from_store"], transfer["sku"])
    receiver = (transfer["to_store"], transfer["sku"])

    # Stock leaves the donor on the decision date.
    if stock[donor] < units:
        raise SystemExit(f"Insufficient donor stock: {donor}")
    stock[donor] -= units

    # It becomes sellable at the receiver the following day.
    incoming[receiver] += units
    donor_keys.add(donor)
    total_moved += units

baseline_test = [
    row for row in baseline
    if start <= date.fromisoformat(row["date"]) <= end
]
baseline_test.sort(key=lambda row: (
    row["date"], row["store_id"], row["sku"]
))
assert len(baseline_test) == 18000

results = []
donor_days_made_worse = 0
receiver_day = start.isoformat()

for row in baseline_test:
    key = (row["store_id"], row["sku"])
    opening = stock[key]
    supplier_receipt = int(row["received_units"])
    transfer_receipt = (
        incoming[key] if row["date"] == receiver_day else 0
    )
    available = opening + supplier_receipt + transfer_receipt
    requested = int(row["requested_units"])
    fulfilled = min(available, requested)
    unmet = requested - fulfilled
    stock[key] = available - fulfilled

    if key in donor_keys and unmet > int(row["unmet_units"]):
        donor_days_made_worse += 1

    assert stock[key] >= 0
    assert opening + supplier_receipt + transfer_receipt - fulfilled == stock[key]

    results.append({
        "date": row["date"],
        "store_id": key[0],
        "sku": key[1],
        "requested_units": requested,
        "fulfilled_units": fulfilled,
        "unmet_units": unmet,
        "supplier_received_units": supplier_receipt,
        "transfer_received_units": transfer_receipt,
        "closing_units": stock[key],
    })

with (processed / "transfer_replay_daily.csv").open(
    "w", newline="", encoding="utf-8"
) as file:
    writer = csv.DictWriter(file, fieldnames=list(results[0]))
    writer.writeheader()
    writer.writerows(results)

def measure(rows):
    requested = sum(int(r["requested_units"]) for r in rows)
    fulfilled = sum(int(r["fulfilled_units"]) for r in rows)
    stockouts = sum(int(r["unmet_units"]) > 0 for r in rows)
    value = sum(
        int(r["closing_units"])
        * float(products[r["sku"]]["unit_cost_usd"])
        for r in rows
    ) / 15
    return {
        "fill_rate_percent": round(100 * fulfilled / requested, 2),
        "stockout_store_sku_days": stockouts,
        "average_daily_inventory_value_usd": round(value, 2),
    }

old = measure(baseline_test)
new = measure(results)
report = {
    "test_window": "2026-02-15 through 2026-03-01",
    "transfers": len(transfers),
    "units_moved": total_moved,
    "donor_store_medicine_days_with_more_unmet_demand":
        donor_days_made_worse,
    "baseline": old,
    "baseline_plus_transfers": new,
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
    "limit": (
        "Supplier receipts follow the fixed baseline schedule; "
        "transport cost and expiry are not yet modeled."
    ),
}
(reports / "10_transfer_impact.json").write_text(
    json.dumps(report, indent=2), encoding="utf-8"
)
print(json.dumps(report, indent=2))