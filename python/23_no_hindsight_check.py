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


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


lock = json.loads(
    (root / "docs" / "locked_policy_v2.json").read_text(
        encoding="utf-8"
    )
)
march_selection = json.loads(
    (reports / "21_march_policy_selection.json").read_text(
        encoding="utf-8"
    )
)
original_report = json.loads(
    (reports / "22_april_final_evaluation.json").read_text(
        encoding="utf-8"
    )
)

cap = lock["daily_escalation_limit"]
assert cap == march_selection["chosen_daily_escalation_limit"]
assert cap == original_report["locked_policy"]["daily_escalation_limit"]
assert lock["locked_after"] == "2026-03-31"

products = {
    row["sku"]: row
    for row in read_csv(root / "data" / "raw" / "products.csv")
}
daily = read_csv(v2 / "baseline_daily_120d.csv")
orders = read_csv(v2 / "purchase_orders_120d.csv")

stock_at_cutoff = {
    (row["store_id"], row["sku"]): int(row["closing_units"])
    for row in daily
    if row["date"] == cutoff.isoformat()
}
assert len(stock_at_cutoff) == 1200

# Priority remains fixed using information through March 1.
historical_unmet = defaultdict(int)
for row in daily:
    if row["date"] <= cutoff.isoformat():
        historical_unmet[(row["store_id"], row["sku"])] += int(
            row["unmet_units"]
        )

# On the promised day, an order is eligible if it has not arrived.
# We do not exclude an order because its eventual delivery happens
# to be the next day: the buyer cannot know that in advance.
eligible_by_day = defaultdict(list)
for po in orders:
    placed = date.fromisoformat(po["order_date"])
    expected = date.fromisoformat(po["expected_date"])
    actual = date.fromisoformat(po["actual_receipt_date"])

    if placed > april_end or actual <= cutoff:
        continue
    if actual <= expected:
        continue

    decision_day = max(expected, cutoff)
    next_day = decision_day + timedelta(days=1)
    if next_day > april_end:
        continue

    key = (po["store_id"], po["sku"])
    eligible_by_day[decision_day].append(
        (historical_unmet[key], po, next_day)
    )

selected = {}
audit_rows = []

for day in sorted(eligible_by_day):
    candidates = sorted(
        eligible_by_day[day],
        key=lambda item: (-item[0], item[1]["po_id"])
    )

    for risk, po, next_day in candidates[:cap]:
        original = date.fromisoformat(
            po["actual_receipt_date"]
        )
        effective = min(original, next_day)

        selected[po["po_id"]] = effective
        audit_rows.append({
            "po_id": po["po_id"],
            "store_id": po["store_id"],
            "sku": po["sku"],
            "decision_date": day.isoformat(),
            "historical_unmet_units": risk,
            "original_receipt_date": original.isoformat(),
            "policy_receipt_date": effective.isoformat(),
            "arrival_changed": int(effective < original),
            "assumed_escalation_cost_usd": 25,
        })

baseline_arrivals = defaultdict(int)
policy_arrivals = defaultdict(int)

for po in orders:
    if date.fromisoformat(po["order_date"]) > april_end:
        continue

    original = date.fromisoformat(
        po["actual_receipt_date"]
    )
    effective = selected.get(po["po_id"], original)
    units = int(po["ordered_units"])
    key = (po["store_id"], po["sku"])

    if cutoff < original <= april_end:
        baseline_arrivals[
            (original.isoformat(), key[0], key[1])
        ] += units

    if cutoff < effective <= april_end:
        policy_arrivals[
            (effective.isoformat(), key[0], key[1])
        ] += units

baseline_stock = stock_at_cutoff.copy()
policy_stock = stock_at_cutoff.copy()

def blank():
    return {
        "requested": 0,
        "baseline_fulfilled": 0,
        "policy_fulfilled": 0,
        "baseline_stockout_days": 0,
        "policy_stockout_days": 0,
        "baseline_inventory_sum": 0.0,
        "policy_inventory_sum": 0.0,
        "rows": 0,
    }


totals = {"march": blank(), "april": blank()}
april_rows = []

replay = [
    row for row in daily
    if cutoff.isoformat() < row["date"] <= april_end.isoformat()
]
replay.sort(
    key=lambda row: (
        row["date"], row["store_id"], row["sku"]
    )
)
assert len(replay) == 72000

for row in replay:
    day = row["date"]
    key = (row["store_id"], row["sku"])
    event = (day, key[0], key[1])
    requested = int(row["requested_units"])
    baseline_received = baseline_arrivals[event]
    policy_received = policy_arrivals[event]

    # Verify that the saved baseline is reproduced exactly.
    assert baseline_stock[key] == int(row["opening_units"])
    assert baseline_received == int(row["received_units"])

    baseline_stock[key] += baseline_received
    baseline_fulfilled = min(
        baseline_stock[key], requested
    )
    baseline_stock[key] -= baseline_fulfilled

    assert baseline_fulfilled == int(row["fulfilled_units"])
    assert baseline_stock[key] == int(row["closing_units"])

    policy_stock[key] += policy_received
    policy_fulfilled = min(policy_stock[key], requested)
    policy_stock[key] -= policy_fulfilled
    assert policy_stock[key] >= 0

    month = "march" if day <= march_end.isoformat() else "april"
    bucket = totals[month]
    unit_cost = float(products[key[1]]["unit_cost_usd"])

    bucket["requested"] += requested
    bucket["baseline_fulfilled"] += baseline_fulfilled
    bucket["policy_fulfilled"] += policy_fulfilled
    bucket["baseline_stockout_days"] += (
        baseline_fulfilled < requested
    )
    bucket["policy_stockout_days"] += (
        policy_fulfilled < requested
    )
    bucket["baseline_inventory_sum"] += (
        baseline_stock[key] * unit_cost
    )
    bucket["policy_inventory_sum"] += (
        policy_stock[key] * unit_cost
    )
    bucket["rows"] += 1

    if month == "april":
        april_rows.append({
            "date": day,
            "store_id": key[0],
            "sku": key[1],
            "requested_units": requested,
            "baseline_fulfilled_units": baseline_fulfilled,
            "policy_fulfilled_units": policy_fulfilled,
            "baseline_unmet_units": requested - baseline_fulfilled,
            "policy_unmet_units": requested - policy_fulfilled,
            "baseline_closing_units": baseline_stock[key],
            "policy_closing_units": policy_stock[key],
            "unit_cost_usd": unit_cost,
        })

assert totals["march"]["rows"] == 36000
assert totals["april"]["rows"] == 36000


def summarize(bucket):
    baseline_fill = (
        100 * bucket["baseline_fulfilled"] / bucket["requested"]
    )
    policy_fill = (
        100 * bucket["policy_fulfilled"] / bucket["requested"]
    )
    baseline_value = bucket["baseline_inventory_sum"] / 30
    policy_value = bucket["policy_inventory_sum"] / 30

    return {
        "baseline_fill_rate_percent": round(baseline_fill, 2),
        "policy_fill_rate_percent": round(policy_fill, 2),
        "fill_rate_change_percentage_points": round(
            policy_fill - baseline_fill, 2
        ),
        "baseline_stockout_store_sku_days": (
            bucket["baseline_stockout_days"]
        ),
        "policy_stockout_store_sku_days": (
            bucket["policy_stockout_days"]
        ),
        "stockout_days_change_percent": round(
            100 * (
                bucket["policy_stockout_days"]
                / bucket["baseline_stockout_days"] - 1
            ),
            2,
        ),
        "baseline_average_daily_inventory_value_usd": round(
            baseline_value, 2
        ),
        "policy_average_daily_inventory_value_usd": round(
            policy_value, 2
        ),
        "inventory_value_change_percent": round(
            100 * (policy_value / baseline_value - 1),
            2,
        ),
    }


march = summarize(totals["march"])
april = summarize(totals["april"])

# The correction must not silently change the baseline.
old_april = original_report["april"]
assert april["baseline_fill_rate_percent"] == (
    old_april["baseline_fill_rate_percent"]
)
assert april["baseline_stockout_store_sku_days"] == (
    old_april["baseline_stockout_store_sku_days"]
)
assert april["baseline_average_daily_inventory_value_usd"] == (
    old_april["baseline_average_daily_inventory_value_usd"]
)

april_escalations = sum(
    april_start.isoformat() <= row["decision_date"]
    <= april_end.isoformat()
    for row in audit_rows
)

result = {
    "project": "Pharmacy ShelfGuard synthetic simulation",
    "analysis": "No-hindsight sensitivity check",
    "locked_daily_escalation_limit": cap,
    "change_from_step_22": (
        "An overdue order is considered for escalation without "
        "using its eventual receipt date to predict whether "
        "escalation will help. If it arrives normally the next "
        "day, the escalation still incurs the assumed cost."
    ),
    "march": march,
    "april": april,
    "april_orders_escalated": april_escalations,
    "estimated_april_escalation_cost_usd": (
        25 * april_escalations
    ),
    "original_april_policy_fill_rate_percent": (
        old_april["policy_fill_rate_percent"]
    ),
    "validation": (
        "Passed: baseline reproduced exactly and April baseline "
        "matches the original report."
    ),
    "limitations": (
        "Supplier next-day escalation and its $25 cost are "
        "assumptions. Purchase orders remain on the baseline "
        "schedule despite different policy inventory. April "
        "was already examined in Step 22, so this is a "
        "sensitivity check, not a new untouched holdout."
    ),
}

write_csv(
    v2 / "policy_comparison_april_no_hindsight.csv",
    april_rows,
)
write_csv(
    reports / "23_escalation_decisions.csv",
    audit_rows,
)
(reports / "23_no_hindsight_sensitivity.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)

print(json.dumps(result, indent=2))
print("\nSaved: reports/23_no_hindsight_sensitivity.json")
print("Saved: reports/23_escalation_decisions.csv")
print(
    "Saved: data/v2/processed/"
    "policy_comparison_april_no_hindsight.csv"
)