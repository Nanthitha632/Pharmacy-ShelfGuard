"""Issue synthetic purchase-order documents from the capped buyer worklist."""

from contextlib import closing as close_connection
from datetime import date, timedelta
from math import ceil

QUEUE_RULE = "april_p90_priority_review_v1"
ISSUE_RULE = "simulated_autoapproval_april_p90_v1"


def issue(day_text, connection):
    day = date.fromisoformat(day_text)
    with close_connection(connection) as conn, conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(348725, 5)")
        cur.execute("""
            SELECT q.store_id, q.sku, q.recommended_units,
                   q.estimated_cost_usd, q.budget_reference_usd,
                   r.supplier_id, r.mean_lead_days, p.case_pack
            FROM shelfguard.po_approval_queue_v3 q
            JOIN shelfguard.order_recommendations_v3 r
              ON r.as_of_date = q.as_of_date
             AND r.store_id = q.store_id AND r.sku = q.sku
             AND r.method = 'lead_time_safety_stock_v1'
            JOIN shelfguard.products p ON p.sku = q.sku
            WHERE q.as_of_date = %s AND q.method = %s
              AND q.queue_status = 'PRIORITY_BUYER_REVIEW'
            ORDER BY q.priority_rank
        """, (day, QUEUE_RULE))
        choices = cur.fetchall()
        if not choices:
            raise ValueError("No priority lines to turn into simulated POs")
        budget = choices[0][4]
        if any(row[4] != budget for row in choices):
            raise ValueError("Mixed budget references")

        planned = {}
        for store, sku, units, cost, _, supplier, lead, pack in choices:
            if units <= 0 or units % pack or lead <= 0:
                raise ValueError(f"Invalid purchase proposal: {store}/{sku}")
            po_id = f"SG{day:%Y%m%d}{store}{sku}"
            expected = day + timedelta(days=max(1, ceil(lead)))
            if po_id in planned:
                raise ValueError(f"Duplicate PO ID: {po_id}")
            planned[po_id] = (
                day, expected, store, sku, supplier, units, cost,
                "SIMULATED_OPEN", ISSUE_RULE,
            )
        total = sum(values[6] for values in planned.values())
        if total > budget:
            raise ValueError("Selected spend exceeds budget reference")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shelfguard.purchase_orders_v3 (
                po_id TEXT PRIMARY KEY,
                order_date DATE NOT NULL,
                expected_date DATE NOT NULL,
                store_id TEXT NOT NULL REFERENCES shelfguard.stores(store_id),
                sku TEXT NOT NULL REFERENCES shelfguard.products(sku),
                supplier_id TEXT NOT NULL
                    REFERENCES shelfguard.suppliers(supplier_id),
                ordered_units INTEGER NOT NULL CHECK (ordered_units > 0),
                estimated_cost_usd NUMERIC(14,2) NOT NULL,
                po_status TEXT NOT NULL CHECK (po_status = 'SIMULATED_OPEN'),
                approval_rule TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CHECK (expected_date > order_date),
                UNIQUE (order_date, store_id, sku)
            )
        """)
        cur.executemany("""
            INSERT INTO shelfguard.purchase_orders_v3
                (po_id, order_date, expected_date, store_id, sku,
                 supplier_id, ordered_units, estimated_cost_usd,
                 po_status, approval_rule)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (po_id) DO NOTHING
        """, [(po_id, *values) for po_id, values in planned.items()])
        cur.execute("""
            SELECT po_id, order_date, expected_date, store_id, sku,
                   supplier_id, ordered_units, estimated_cost_usd,
                   po_status, approval_rule
            FROM shelfguard.purchase_orders_v3
            WHERE order_date = %s AND approval_rule = %s
        """, (day, ISSUE_RULE))
        saved = {po_id: tuple(values) for po_id, *values in cur.fetchall()}
        if saved != planned:
            raise ValueError("PO documents changed on rerun; rolled back")

    earliest = min(values[1] for values in planned.values())
    print(
        f"PASS: {day_text}: {len(planned)} simulated PO documents; "
        f"estimated value ${total:,.2f} under ${budget:,.2f}; "
        f"first expected receipt {earliest}. "
        "Approval is a scenario rule; no buyer signed off, "
        "no supplier message sent, and no real purchase made."
    )
    return day_text
