"""Prioritize synthetic PO proposals for buyer review, without approvals."""

from contextlib import closing as close_connection
from decimal import Decimal
from pathlib import Path
import json

REFERENCE = Path("/opt/shelfguard/reports/27_budget_reference.json")
METHOD = "april_p90_priority_review_v1"


def build(day_text, connection):
    reference = json.loads(REFERENCE.read_text(encoding="utf-8-sig"))
    cap = Decimal(str(reference["p90_daily_spend_usd"]))
    if cap <= 0:
        raise ValueError("Budget reference must be positive")

    with close_connection(connection) as conn, conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(348725, 4)")
        cur.execute("""
            SELECT r.store_id, r.sku, r.recommended_units,
                   r.estimated_cost_usd, r.closing_units,
                   r.forecast_units, r.overdue_units, i.unmet_units
            FROM shelfguard.order_recommendations_v3 r
            JOIN shelfguard.daily_inventory_v3 i
              ON i.inventory_date = r.as_of_date
             AND i.store_id = r.store_id AND i.sku = r.sku
            WHERE r.as_of_date = %s
              AND r.method = 'lead_time_safety_stock_v1'
            ORDER BY r.store_id, r.sku
        """, (day_text,))
        rows = cur.fetchall()
        if len(rows) != 1200 or len({(r[0], r[1]) for r in rows}) != 1200:
            raise ValueError("Expected 1,200 unique recommendation and inventory pairs")

        planned = {}
        candidates = []
        for store, sku, units, cost, closing, forecast, overdue, unmet in rows:
            days = (Decimal(closing) / max(Decimal(forecast), Decimal(1)))
            days_saved = days.quantize(Decimal("0.01"))
            if units == 0:
                status = "NO_ORDER"
            elif overdue > 0:
                status = "ESCALATE_OVERDUE_PO"
            else:
                status = None
                candidates.append((
                    -int(unmet > 0), -unmet, days, -forecast,
                    store, sku, cost,
                ))
            planned[(store, sku)] = [
                status, None, units, cost, days_saved, unmet, overdue, cap
            ]

        remaining = cap
        for rank, (_, _, _, _, store, sku, cost) in enumerate(
            sorted(candidates), start=1
        ):
            item = planned[(store, sku)]
            item[1] = rank
            if cost <= remaining:
                item[0] = "PRIORITY_BUYER_REVIEW"
                remaining -= cost
            else:
                item[0] = "DEFER_BUDGET"

        selected = sum(
            item[3] for item in planned.values()
            if item[0] == "PRIORITY_BUYER_REVIEW"
        )
        if selected > cap or any(item[0] is None for item in planned.values()):
            raise ValueError("Invalid procurement triage result")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shelfguard.po_approval_queue_v3 (
                as_of_date DATE NOT NULL,
                store_id TEXT NOT NULL REFERENCES shelfguard.stores(store_id),
                sku TEXT NOT NULL REFERENCES shelfguard.products(sku),
                method TEXT NOT NULL,
                queue_status TEXT NOT NULL CHECK (queue_status IN (
                    'NO_ORDER', 'ESCALATE_OVERDUE_PO',
                    'PRIORITY_BUYER_REVIEW', 'DEFER_BUDGET'
                )),
                priority_rank INTEGER,
                recommended_units INTEGER NOT NULL,
                estimated_cost_usd NUMERIC(14,2) NOT NULL,
                days_of_supply NUMERIC(12,2) NOT NULL,
                unmet_units INTEGER NOT NULL,
                overdue_units INTEGER NOT NULL,
                budget_reference_usd NUMERIC(14,2) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (as_of_date, store_id, sku, method)
            )
        """)
        cur.executemany("""
            INSERT INTO shelfguard.po_approval_queue_v3 (
                as_of_date, store_id, sku, method, queue_status,
                priority_rank, recommended_units, estimated_cost_usd,
                days_of_supply, unmet_units, overdue_units,
                budget_reference_usd
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (as_of_date, store_id, sku, method) DO NOTHING
        """, [
            (day_text, store, sku, METHOD, *item)
            for (store, sku), item in sorted(planned.items())
        ])
        cur.execute("""
            SELECT store_id, sku, queue_status, priority_rank,
                   recommended_units, estimated_cost_usd,
                   days_of_supply, unmet_units, overdue_units,
                   budget_reference_usd
            FROM shelfguard.po_approval_queue_v3
            WHERE as_of_date = %s AND method = %s
        """, (day_text, METHOD))
        saved = {
            (store, sku): list(values)
            for store, sku, *values in cur.fetchall()
        }
        if saved != planned:
            raise ValueError("Queue changed on rerun; transaction rolled back")

    count = lambda status: sum(item[0] == status for item in planned.values())
    print(
        f"PASS: {day_text}: budget reference ${cap:,.2f}; "
        f"{count('PRIORITY_BUYER_REVIEW')} buyer-review lines "
        f"(${selected:,.2f}); "
        f"{count('ESCALATE_OVERDUE_PO')} overdue-supplier lines; "
        f"{count('DEFER_BUDGET')} deferred for budget. "
        "No order approved or placed."
    )
    return day_text
