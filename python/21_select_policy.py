from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import csv
import json

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
v2 = root / "data" / "v2" / "processed"
reports = root / "reports"
cutoff = date(2026, 3, 1)
start = date(2026, 3, 2)
end = date(2026, 3, 31)

def load(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

products = {
    r["sku"]: r for r in load(raw / "products.csv")
}
daily = load(v2 / "baseline_daily_120d.csv")
orders = load(v2 / "purchase_orders_120d.csv")

starting_stock = {
    (r["store_id"], r["sku"]): int(r["closing_units"])
    for r in daily if r["date"] == cutoff.isoformat()
}
march = [
    r for r in daily
    if start <= date.fromisoformat(r["date"]) <= end
]
march.sort(key=lambda r: (r["date"], r["store_id"], r["sku"]))
assert len(starting_stock) == 1200
assert len(march) == 36000

# Risk ranking is learned only from dates through March 1.
past_unmet = defaultdict(int)
for row in daily:
    if date.fromisoformat(row["date"]) <= cutoff:
        past_unmet[(row["store_id"], row["sku"])] += int(
            row["unmet_units"]
        )

eligible_by_day = defaultdict(list)
for po in orders:
    placed = date.fromisoformat(po["order_date"])
    expected = date.fromisoformat(po["expected_date"])
    original = date.fromisoformat(po["actual_receipt_date"])

    if placed > end or original <= cutoff:
        continue
    if original <= expected:
        continue

    # The team knows an order is overdue only after the expected
    # day passes without a goods receipt.
    escalation_day = max(expected, cutoff)
    accelerated = escalation_day + timedelta(days=1)
    if accelerated >= original or accelerated > end:
        continue

    risk = past_unmet[(po["store_id"], po["sku"])]
    eligible_by_day[escalation_day].append(
        (risk, po, accelerated)
    )

for day in eligible_by_day:
    eligible_by_day[day].sort(
        key=lambda item: (
            -item[0],
            item[1]["po_id"],
        )
    )

def evaluate(daily_cap):
    selected = {}
    for day, candidates in eligible_by_day.items():
        for _, po, accelerated in candidates[:daily_cap]:
            selected[po["po_id"]] = accelerated

    arrivals = defaultdict(int)
    for po in orders:
        if date.fromisoformat(po["order_date"]) > end:
            continue
        arrival = selected.get(
            po["po_id"],
            date.fromisoformat(po["actual_receipt_date"]),
        )
        if start <= arrival <= end:
            arrivals[
                (arrival.isoformat(),
                 po["store_id"], po["sku"])
            ] += int(po["ordered_units"])

    stock = starting_stock.copy()
    requested_total = 0
    fulfilled_total = 0
    stockout_days = 0
    closing_value_sum = 0.0

    for row in march:
        key = (row["store_id"], row["sku"])
        event = (row["date"], key[0], key[1])
        requested = int(row["requested_units"])
        stock[key] += arrivals[event]
        fulfilled = min(stock[key], requested)
        stock[key] -= fulfilled

        requested_total += requested
        fulfilled_total += fulfilled
        stockout_days += fulfilled < requested
        closing_value_sum += (
            stock[key]
            * float(products[key[1]]["unit_cost_usd"])
        )
        assert stock[key] >= 0

    return {
        "daily_escalation_limit": daily_cap,
        "orders_escalated": len(selected),
        "estimated_escalation_cost_usd": 25 * len(selected),
        "fill_rate_percent": round(
            100 * fulfilled_total / requested_total, 2
        ),
        "stockout_store_sku_days": stockout_days,
        "average_daily_inventory_value_usd": round(
            closing_value_sum / 30, 2
        ),
    }

candidates = [
    evaluate(limit) for limit in [0, 5, 15, 50]
]
baseline = candidates[0]
for row in candidates:
    row["inventory_change_percent"] = round(
        100 * (
            row["average_daily_inventory_value_usd"]
            / baseline["average_daily_inventory_value_usd"]
            - 1
        ), 2
    )

eligible = [
    row for row in candidates
    if row["inventory_change_percent"] <= 2.0
]
chosen = max(
    eligible,
    key=lambda row: (
        row["fill_rate_percent"],
        -row["estimated_escalation_cost_usd"],
    ),
)

selection = {
    "selection_window": "2026-03-02 through 2026-03-31",
    "comparison": candidates,
    "chosen_daily_escalation_limit":
        chosen["daily_escalation_limit"],
    "selection_rule": (
        "Highest March fill rate among candidates with "
        "inventory value no more than 2% above March baseline; "
        "lower escalation cost breaks a tie"
    ),
    "assumption": (
        "Escalated overdue orders can arrive the next day "
        "at $25 per order; supplier capacity is simulated"
    ),
}
(reports / "21_march_policy_selection.json").write_text(
    json.dumps(selection, indent=2), encoding="utf-8"
)

locked = {
    "policy": "Escalate overdue supplier orders",
    "locked_after": "2026-03-31",
    "daily_escalation_limit":
        chosen["daily_escalation_limit"],
    "priority": (
        "Historical unmet units at the store and SKU "
        "through 2026-03-01; PO ID breaks ties"
    ),
    "final_evaluation": "2026-04-01 through 2026-04-30",
    "rule": "Do not change this limit using April outcomes",
}
(root / "docs" / "locked_policy_v2.json").write_text(
    json.dumps(locked, indent=2), encoding="utf-8"
)

print(json.dumps(selection, indent=2))
print("\nPolicy saved: docs/locked_policy_v2.json")