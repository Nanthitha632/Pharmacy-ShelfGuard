from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import json

root = Path(__file__).resolve().parents[1]
v2 = root / "data" / "v2" / "processed"
reports = root / "reports"

cutoff = date(2026, 3, 1)
march_end = date(2026, 3, 31)
april_start = date(2026, 4, 1)
april_end = date(2026, 4, 30)


def load_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def save_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


lock = json.loads(
    (root / "docs" / "locked_policy_v2.json").read_text(
        encoding="utf-8"
    )
)
selection = json.loads(
    (reports / "21_march_policy_selection.json").read_text(
        encoding="utf-8"
    )
)

cap = lock["daily_escalation_limit"]
assert isinstance(cap, int) and cap >= 0
assert cap == selection["chosen_daily_escalation_limit"]
assert lock["locked_after"] == march_end.isoformat()
assert lock["final_evaluation"] == (
    "2026-04-01 through 2026-04-30"
)

products = {
    row["sku"]: row
    for row in load_csv(root / "data" / "raw" / "products.csv")
}
daily = load_csv(v2 / "baseline_daily_120d.csv")
orders = load_csv(v2 / "purchase_orders_120d.csv")

starting_stock = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in daily
    if row["date"] == cutoff.isoformat()
}
assert len(starting_stock) == 1200

# Priority scores use historical unmet demand through March 1 only.
past_unmet = defaultdict(int)
for row in daily:
    if row["date"] <= cutoff.isoformat():
        past_unmet[(row["store_id"], row["sku"])] += int(
            row["unmet_units"]
        )

eligible_by_day = defaultdict(list)
for po in orders:
    placed = date.fromisoformat(po["order_date"])
    expected = date.fromisoformat(po["expected_date"])
    original = date.fromisoformat(po["actual_receipt_date"])

    if placed > april_end or original <= cutoff:
        continue
    if original <= expected:
        continue

    escalation_day = max(expected, cutoff)
    accelerated = escalation_day + timedelta(days=1)

    if accelerated >= original or accelerated > april_end:
        continue

    key = (po["store_id"], po["sku"])
    risk = past_unmet[key]
    eligible_by_day[escalation_day].append(
        (risk, po, accelerated)
    )

selected = {}
for day, candidates in eligible_by_day.items():
    candidates.sort(
        key=lambda item: (-item[0], item[1]["po_id"])
    )
    for risk, po, accelerated in candidates[:cap]:
        selected[po["po_id"]] = {
            "escalation_day": day,
            "accelerated_date": accelerated,
            "original_date": date.fromisoformat(
                po["actual_receipt_date"]
            ),
            "risk_score": risk,
        }

baseline_arrivals = defaultdict(int)
policy_arrivals = defaultdict(int)

for po in orders:
    if date.fromisoformat(po["order_date"]) > april_end:
        continue

    key = (po["store_id"], po["sku"])
    units = int(po["ordered_units"])
    original = date.fromisoformat(po["actual_receipt_date"])
    policy_date = selected.get(po["po_id"], {}).get(
        "accelerated_date", original
    )

    if cutoff < original <= april_end:
        baseline_arrivals[
            (original.isoformat(), key[0], key[1])
        ] += units
    if cutoff < policy_date <= april_end:
        policy_arrivals[
            (policy_date.isoformat(), key[0], key[1])
        ] += units

baseline_stock = starting_stock.copy()
policy_stock = starting_stock.copy()

def empty_totals():
    return {
        "requested": 0,
        "baseline_fulfilled": 0,
        "policy_fulfilled": 0,
        "baseline_stockout_days": 0,
        "policy_stockout_days": 0,
        "baseline_inventory_value_sum": 0.0,
        "policy_inventory_value_sum": 0.0,
        "rows": 0,
    }


totals = {
    "march": empty_totals(),
    "april": empty_totals(),
}
april_rows = []

replay_rows = [
    row for row in daily
    if cutoff.isoformat() < row["date"] <= april_end.isoformat()
]
replay_rows.sort(
    key=lambda row: (
        row["date"], row["store_id"], row["sku"]
    )
)
assert len(replay_rows) == 72000

for row in replay_rows:
    day = row["date"]
    key = (row["store_id"], row["sku"])
    event = (day, key[0], key[1])
    month = "march" if day <= march_end.isoformat() else "april"
    bucket = totals[month]

    requested = int(row["requested_units"])
    baseline_received = baseline_arrivals[event]
    policy_received = policy_arrivals[event]

    # A zero-change replay must reproduce the saved baseline.
    assert baseline_stock[key] == int(row["opening_units"])
    assert baseline_received == int(row["received_units"])

    baseline_stock[key] += baseline_received
    baseline_fulfilled = min(baseline_stock[key], requested)
    baseline_stock[key] -= baseline_fulfilled

    assert baseline_fulfilled == int(row["fulfilled_units"])
    assert baseline_stock[key] == int(row["closing_units"])

    policy_opening = policy_stock[key]
    policy_stock[key] += policy_received
    policy_fulfilled = min(policy_stock[key], requested)
    policy_stock[key] -= policy_fulfilled

    assert policy_stock[key] >= 0
    assert (
        policy_opening + policy_received - policy_fulfilled
        == policy_stock[key]
    )

    cost = float(products[key[1]]["unit_cost_usd"])
    bucket["requested"] += requested
    bucket["baseline_fulfilled"] += baseline_fulfilled
    bucket["policy_fulfilled"] += policy_fulfilled
    bucket["baseline_stockout_days"] += (
        baseline_fulfilled < requested
    )
    bucket["policy_stockout_days"] += (
        policy_fulfilled < requested
    )
    bucket["baseline_inventory_value_sum"] += (
        baseline_stock[key] * cost
    )
    bucket["policy_inventory_value_sum"] += (
        policy_stock[key] * cost
    )
    bucket["rows"] += 1

    if month == "april":
        april_rows.append({
            "date": day,
            "store_id": key[0],
            "sku": key[1],
            "requested_units": requested,
            "baseline_received_units": baseline_received,
            "policy_received_units": policy_received,
            "baseline_fulfilled_units": baseline_fulfilled,
            "policy_fulfilled_units": policy_fulfilled,
            "baseline_unmet_units": requested - baseline_fulfilled,
            "policy_unmet_units": requested - policy_fulfilled,
            "baseline_closing_units": baseline_stock[key],
            "policy_closing_units": policy_stock[key],
            "unit_cost_usd": cost,
        })

assert totals["march"]["rows"] == 36000
assert totals["april"]["rows"] == 36000


def measures(bucket, days):
    baseline_fill = (
        100 * bucket["baseline_fulfilled"] / bucket["requested"]
    )
    policy_fill = (
        100 * bucket["policy_fulfilled"] / bucket["requested"]
    )
    baseline_inventory = (
        bucket["baseline_inventory_value_sum"] / days
    )
    policy_inventory = (
        bucket["policy_inventory_value_sum"] / days
    )
    baseline_stockouts = bucket["baseline_stockout_days"]
    policy_stockouts = bucket["policy_stockout_days"]

    return {
        "requested_units": bucket["requested"],
        "baseline_fill_rate_percent": round(baseline_fill, 2),
        "policy_fill_rate_percent": round(policy_fill, 2),
        "fill_rate_change_percentage_points": round(
            policy_fill - baseline_fill, 2
        ),
        "baseline_stockout_store_sku_days": baseline_stockouts,
        "policy_stockout_store_sku_days": policy_stockouts,
        "stockout_days_change_percent": round(
            100 * (policy_stockouts / baseline_stockouts - 1),
            2,
        ),
        "baseline_average_daily_inventory_value_usd": round(
            baseline_inventory, 2
        ),
        "policy_average_daily_inventory_value_usd": round(
            policy_inventory, 2
        ),
        "inventory_value_change_percent": round(
            100 * (policy_inventory / baseline_inventory - 1),
            2,
        ),
    }


march = measures(totals["march"], 30)

# Confirm the replay matches the March result used to select the policy.
chosen_march = next(
    row for row in selection["comparison"]
    if row["daily_escalation_limit"] == cap
)
assert march["policy_fill_rate_percent"] == (
    chosen_march["fill_rate_percent"]
)
assert march["policy_stockout_store_sku_days"] == (
    chosen_march["stockout_store_sku_days"]
)
assert march["policy_average_daily_inventory_value_usd"] == (
    chosen_march["average_daily_inventory_value_usd"]
)

april = measures(totals["april"], 30)
march_escalations = sum(
    item["escalation_day"] <= march_end
    for item in selected.values()
)
april_escalations = sum(
    april_start <= item["escalation_day"] <= april_end
    for item in selected.values()
)

result = {
    "project": "Pharmacy ShelfGuard synthetic simulation",
    "evaluation_window": "2026-04-01 through 2026-04-30",
    "locked_policy": {
        "action": lock["policy"],
        "daily_escalation_limit": cap,
        "priority": lock["priority"],
    },
    "validation": (
        "Passed: baseline replay matches saved daily inventory; "
        "March policy replay matches the March selection report."
    ),
    "april": april,
    "orders_escalated_in_march": march_escalations,
    "orders_escalated_in_april": april_escalations,
    "estimated_april_escalation_cost_usd": (
        25 * april_escalations
    ),
    "interpretation_note": (
        "An escalation assumes next-day receipt at $25 per order. "
        "The original baseline purchase-order schedule stays fixed "
        "even when policy inventory changes. These are simulated "
        "counterfactual results, not observed pharmacy outcomes."
    ),
}

save_csv(v2 / "policy_comparison_april.csv", april_rows)
(reports / "22_april_final_evaluation.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)

print(json.dumps(result, indent=2))
print(
    "\nSaved: reports/22_april_final_evaluation.json"
    "\nSaved: data/v2/processed/policy_comparison_april.csv"
)