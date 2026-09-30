"""Post simulated goods receipts and balance store inventory for one day."""

from collections import defaultdict
from contextlib import closing as close_connection
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
import csv

PO_SOURCE = Path(
    "/opt/shelfguard/data/v2/processed/purchase_orders_120d.csv"
)


def settle(day_text, connection):
    day = date.fromisoformat(day_text)
    if day < date(2026, 5, 1):
        raise ValueError("The new inventory period begins May 1, 2026")
    if not PO_SOURCE.is_file():
        raise FileNotFoundError(PO_SOURCE)

    # The simulator releases a receipt only on its arrival day.
    # Later receipt dates are never passed to the purchasing decision.
    receipts = {}
    received_by_pair = defaultdict(int)
    with PO_SOURCE.open(newline="", encoding="utf-8") as source:
        for po in csv.DictReader(source):
            if po["actual_receipt_date"] != day_text:
                continue
            po_id = po["po_id"]
            units = int(po["ordered_units"])
            if po_id in receipts or units <= 0:
                raise ValueError(f"Invalid receipt for {po_id}")
            receipts[po_id] = (
                day, po["store_id"], po["sku"], units
            )
            received_by_pair[
                (po["store_id"], po["sku"])
            ] += units

    with close_connection(connection) as conn, conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(348725, 2)")
        # Supplier response is released only when this simulated day arrives.
        # The purchasing recommendation never reads a future arrival date.
        cur.execute("""
            SELECT po_id, expected_date, store_id, sku, ordered_units
            FROM shelfguard.purchase_orders_v3
            WHERE order_date < %s AND expected_date <= %s
        """, (day, day))
        for po_id, due, store, sku, units in cur.fetchall():
            token = int.from_bytes(
                sha256(f"supplier-v3|{po_id}".encode()).digest()[:8],
                "big",
            )
            delay = (
                1 + (token // 100) % 3
                if token % 100 < 15 else 0
            )
            if due + timedelta(days=delay) == day:
                if po_id in receipts:
                    raise ValueError(f"Duplicate receipt: {po_id}")
                receipts[po_id] = (day, store, sku, units)
                received_by_pair[(store, sku)] += units
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shelfguard.goods_receipts_v3 (
                po_id TEXT PRIMARY KEY,
                receipt_date DATE NOT NULL,
                store_id TEXT NOT NULL REFERENCES shelfguard.stores(store_id),
                sku TEXT NOT NULL REFERENCES shelfguard.products(sku),
                accepted_units INTEGER NOT NULL
                    CHECK (accepted_units > 0),
                posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shelfguard.daily_inventory_v3 (
                inventory_date DATE NOT NULL,
                store_id TEXT NOT NULL REFERENCES shelfguard.stores(store_id),
                sku TEXT NOT NULL REFERENCES shelfguard.products(sku),
                opening_units INTEGER NOT NULL CHECK (opening_units >= 0),
                received_units INTEGER NOT NULL CHECK (received_units >= 0),
                requested_units INTEGER NOT NULL CHECK (requested_units >= 0),
                fulfilled_units INTEGER NOT NULL CHECK (fulfilled_units >= 0),
                unmet_units INTEGER NOT NULL CHECK (unmet_units >= 0),
                closing_units INTEGER NOT NULL CHECK (closing_units >= 0),
                PRIMARY KEY (inventory_date, store_id, sku),
                CHECK (fulfilled_units + unmet_units = requested_units),
                CHECK (
                    opening_units + received_units - fulfilled_units
                    = closing_units
                )
            )
        """)

        if day == date(2026, 5, 1):
            cur.execute("""
                SELECT store_id, sku, baseline_closing_units
                FROM shelfguard.april_policy_comparison
                WHERE date = DATE '2026-04-30'
            """)
        else:
            cur.execute("""
                SELECT store_id, sku, closing_units
                FROM shelfguard.daily_inventory_v3
                WHERE inventory_date = %s
            """, (day - timedelta(days=1),))
        opening = {
            (store, sku): units
            for store, sku, units in cur.fetchall()
        }

        cur.execute("""
            SELECT store_id, sku, requested_units
            FROM shelfguard.daily_demand_v3
            WHERE demand_date = %s
        """, (day,))
        demand = {
            (store, sku): units
            for store, sku, units in cur.fetchall()
        }
        if len(opening) != 1200 or set(opening) != set(demand):
            raise ValueError(
                f"Incomplete opening stock or demand for {day_text}: "
                f"{len(opening)} stock pairs, {len(demand)} demand pairs"
            )
        if not set(received_by_pair).issubset(opening):
            raise ValueError("A receipt has an unknown store–medicine pair")

        planned = {}
        for key, requested in demand.items():
            start = opening[key]
            received = received_by_pair[key]
            fulfilled = min(requested, start + received)
            unmet = requested - fulfilled
            closing = start + received - fulfilled
            planned[key] = (
                start, received, requested, fulfilled, unmet, closing
            )

        cur.executemany("""
            INSERT INTO shelfguard.goods_receipts_v3
                (po_id, receipt_date, store_id, sku, accepted_units)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (po_id) DO NOTHING
        """, [
            (po_id, *details)
            for po_id, details in sorted(receipts.items())
        ])
        cur.execute("""
            SELECT po_id, receipt_date, store_id, sku, accepted_units
            FROM shelfguard.goods_receipts_v3
            WHERE receipt_date = %s
        """, (day,))
        saved_receipts = {
            po_id: (receipt_day, store, sku, units)
            for po_id, receipt_day, store, sku, units
            in cur.fetchall()
        }
        if saved_receipts != receipts:
            raise ValueError("Saved receipts differ on rerun")

        cur.executemany("""
            INSERT INTO shelfguard.daily_inventory_v3
                (inventory_date, store_id, sku, opening_units,
                 received_units, requested_units, fulfilled_units,
                 unmet_units, closing_units)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (inventory_date, store_id, sku) DO NOTHING
        """, [
            (day, store, sku, *values)
            for (store, sku), values in sorted(planned.items())
        ])
        cur.execute("""
            SELECT store_id, sku, opening_units, received_units,
                   requested_units, fulfilled_units, unmet_units,
                   closing_units
            FROM shelfguard.daily_inventory_v3
            WHERE inventory_date = %s
        """, (day,))
        saved = {
            (store, sku): (
                start, received, requested, fulfilled, unmet, closing
            )
            for store, sku, start, received, requested,
                fulfilled, unmet, closing in cur.fetchall()
        }
        if saved != planned:
            raise ValueError(
                "Saved inventory differs on rerun; "
                "transaction rolled back"
            )

    requested_total = sum(v[2] for v in planned.values())
    fulfilled_total = sum(v[3] for v in planned.values())
    stockout_pairs = sum(v[4] > 0 for v in planned.values())
    print(
        f"PASS: {day_text}: {len(receipts)} supplier receipts; "
        f"{len(planned):,} stock balances; "
        f"{fulfilled_total:,}/{requested_total:,} units fulfilled; "
        f"{stockout_pairs} store-medicine stockouts."
    )
    return day_text
