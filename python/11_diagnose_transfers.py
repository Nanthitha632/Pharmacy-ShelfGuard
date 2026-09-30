from pathlib import Path
from collections import defaultdict
from datetime import date
import csv
import json

root = Path(__file__).resolve().parents[1]
processed = root / "data" / "processed"
reports = root / "reports"

def load(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

transfers = load(processed / "transfer_proposals.csv")
baseline = load(processed / "baseline_daily.csv")
replay = load(processed / "transfer_replay_daily.csv")

start = date(2026, 2, 15)
baseline_by_key = {
    (r["date"], r["store_id"], r["sku"]): r
    for r in baseline
    if date.fromisoformat(r["date"]) >= start
}

donors = {
    (r["from_store"], r["sku"]) for r in transfers
}
receivers = {
    (r["to_store"], r["sku"]) for r in transfers
}

by_store_sku = defaultdict(lambda: {
    "extra_fulfilled_units": 0,
    "extra_unmet_units": 0,
})

for row in replay:
    key = (row["date"], row["store_id"], row["sku"])
    original = baseline_by_key[key]
    store_sku = (row["store_id"], row["sku"])

    fulfilled_change = (
        int(row["fulfilled_units"])
        - int(original["fulfilled_units"])
    )
    by_store_sku[store_sku]["extra_fulfilled_units"] += max(
        fulfilled_change, 0
    )
    by_store_sku[store_sku]["extra_unmet_units"] += max(
        -fulfilled_change, 0
    )

def totals(keys):
    return {
        "extra_fulfilled_units": sum(
            by_store_sku[key]["extra_fulfilled_units"]
            for key in keys
        ),
        "extra_unmet_units": sum(
            by_store_sku[key]["extra_unmet_units"]
            for key in keys
        ),
    }

worst_donors = sorted(
    donors,
    key=lambda key: by_store_sku[key]["extra_unmet_units"],
    reverse=True,
)[:5]

diagnosis = {
    "test_window": "2026-02-15 through 2026-03-01",
    "receiver_store_medicines": totals(receivers),
    "donor_store_medicines": totals(donors),
    "five_most_harmed_donor_pairs": [
        {
            "store_id": store,
            "sku": sku,
            "extra_unmet_units":
                by_store_sku[(store, sku)]["extra_unmet_units"],
        }
        for store, sku in worst_donors
    ],
    "interpretation": (
        "Transfers must be judged by net fulfilled units across "
        "both stores, not receiver benefit alone."
    ),
    "evaluation_boundary": (
        "These future outcomes explain the failed experiment. "
        "Do not use them as inputs to a February 14 decision."
    ),
}

(reports / "11_transfer_diagnosis.json").write_text(
    json.dumps(diagnosis, indent=2), encoding="utf-8"
)
print(json.dumps(diagnosis, indent=2))