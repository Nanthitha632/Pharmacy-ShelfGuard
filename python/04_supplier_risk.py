from pathlib import Path
from collections import defaultdict
from datetime import date
import csv
import json

root = Path(__file__).resolve().parents[1]
processed = root / "data" / "processed"
reports = root / "reports"

def read_csv(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))

orders = read_csv(processed / "baseline_purchase_orders.csv")
receipts = read_csv(processed / "baseline_goods_receipts.csv")
receipts_by_po = {row["po_id"]: row for row in receipts}
as_of = date(2026, 3, 1)  # Final day of the 60-day simulation

by_supplier = defaultdict(lambda: {
    "orders": 0,
    "received": 0,
    "late_receipts": 0,
    "total_lead_days": 0,
    "overdue_open_orders": 0,
})

for order in orders:
    supplier = by_supplier[order["supplier_id"]]
    supplier["orders"] += 1
    receipt = receipts_by_po.get(order["po_id"])
    expected = date.fromisoformat(order["expected_date"])

    if receipt:
        actual = date.fromisoformat(receipt["receipt_date"])
        supplier["received"] += 1
        supplier["total_lead_days"] += (
            actual - date.fromisoformat(order["order_date"])
        ).days
        if actual > expected:
            supplier["late_receipts"] += 1
    elif expected <= as_of:
        supplier["overdue_open_orders"] += 1

rows = []
for supplier_id, values in sorted(by_supplier.items()):
    received = values["received"]
    rows.append({
        "supplier_id": supplier_id,
        "purchase_orders": values["orders"],
        "receipts_within_window": received,
        "late_receipts": values["late_receipts"],
        "late_receipt_rate_percent": round(
            100 * values["late_receipts"] / received, 2
        ) if received else 0,
        "average_actual_lead_days": round(
            values["total_lead_days"] / received, 2
        ) if received else 0,
        "overdue_open_orders_at_window_end":
            values["overdue_open_orders"],
    })

columns = list(rows[0])
with (reports / "04_supplier_risk.csv").open(
    "w", newline="", encoding="utf-8"
) as file:
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)

highest_risk = max(
    rows,
    key=lambda row: (
        row["overdue_open_orders_at_window_end"],
        row["late_receipt_rate_percent"],
    ),
)

summary = {
    "as_of_date": as_of.isoformat(),
    "supplier_count": len(rows),
    "highest_priority_supplier": highest_risk["supplier_id"],
    "reason": "Most overdue open purchase orders; late receipt rate breaks ties",
    "total_late_receipts": sum(r["late_receipts"] for r in rows),
    "total_overdue_open_orders": sum(
        r["overdue_open_orders_at_window_end"] for r in rows
    ),
    "definition": (
        "Late receipt: actual receipt after expected date. "
        "Overdue open order: no receipt by the window end and "
        "expected date on or before that date."
    ),
}
(reports / "04_supplier_risk_summary.json").write_text(
    json.dumps(summary, indent=2), encoding="utf-8"
)

print(json.dumps(summary, indent=2))
print("\nOpen reports/04_supplier_risk.csv to compare all four suppliers.")