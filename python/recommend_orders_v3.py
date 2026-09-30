"""Make auditable case-pack purchase recommendations, without placing POs."""

from collections import defaultdict
from contextlib import closing as close_connection
from datetime import date, timedelta
from decimal import Decimal
from math import ceil, sqrt
from pathlib import Path
import csv

ROOT = Path("/opt/shelfguard/data/v2/processed")
METHOD = "lead_time_safety_stock_v1"


def rows(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def mean(values):
    return sum(values) / len(values)


def std(values):
    avg = mean(values)
    return sqrt(sum((v - avg) ** 2 for v in values) / len(values))


def recommend(day_text, connection):
    day = date.fromisoformat(day_text)
    if day < date(2026, 5, 1):
        raise ValueError("Recommendations begin May 1, 2026")
    po_path = ROOT / "purchase_orders_120d.csv"
    receipt_path = ROOT / "goods_receipts_120d.csv"
    if not po_path.is_file() or not receipt_path.is_file():
        raise FileNotFoundError("The historical PO and receipt snapshots are required")

    orders = {}
    for row in rows(po_path):
        if date.fromisoformat(row["order_date"]) <= day:
            if row["po_id"] in orders:
                raise ValueError("Duplicate historical PO")
            orders[row["po_id"]] = row
    received = {}
    for row in rows(receipt_path):
        receipt_day = date.fromisoformat(row["receipt_date"])
        if receipt_day <= day:
            if row["po_id"] in received:
                raise ValueError("Duplicate historical receipt")
            received[row["po_id"]] = receipt_day

    with close_connection(connection) as conn, conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(348725, 3)")
        cur.execute("""
            SELECT po_id, receipt_date
            FROM shelfguard.goods_receipts_v3
            WHERE receipt_date <= %s
        """, (day,))
        for po_id, receipt_day in cur.fetchall():
            if po_id in received:
                raise ValueError(f"Receipt counted twice: {po_id}")
            received[po_id] = receipt_day

        # Only earlier-day simulated POs were visible when today's
        # purchasing decision was made. Same-day POs are downstream.
        cur.execute("""
            SELECT po_id, order_date, expected_date, store_id, sku,
                   supplier_id, ordered_units
            FROM shelfguard.purchase_orders_v3
            WHERE order_date < %s
        """, (day,))
        for po_id, placed, due, store, sku, supplier, units in cur.fetchall():
            if po_id in orders:
                raise ValueError(f"Duplicate PO ID: {po_id}")
            orders[po_id] = {
                "po_id": po_id,
                "order_date": placed.isoformat(),
                "expected_date": due.isoformat(),
                "store_id": store,
                "sku": sku,
                "supplier_id": supplier,
                "ordered_units": str(units),
            }
        cur.execute("""
            SELECT sku, supplier_id, case_pack, unit_cost_usd
            FROM shelfguard.products
        """)
        products = {
            sku: (supplier, pack, cost)
            for sku, supplier, pack, cost in cur.fetchall()
        }
        cur.execute("""
            SELECT store_id, sku, closing_units
            FROM shelfguard.daily_inventory_v3
            WHERE inventory_date = %s
        """, (day,))
        stock = {(store, sku): units for store, sku, units in cur.fetchall()}
        cur.execute("""
            SELECT store_id, sku, forecast_units
            FROM shelfguard.daily_forecast_v3
            WHERE as_of_date = %s AND target_date = %s
              AND method = 'weekday_blend_28d_v1'
        """, (day, day + timedelta(days=1)))
        forecast = {(store, sku): units for store, sku, units in cur.fetchall()}
        if len(stock) != 1200 or set(stock) != set(forecast):
            raise ValueError("Inventory and next-day forecast must cover 1,200 matching pairs")

        start = day - timedelta(days=27)
        cur.execute("""
            SELECT date, store_id, sku, requested_units
            FROM shelfguard.april_policy_comparison
            WHERE date BETWEEN %s AND %s
            UNION ALL
            SELECT demand_date, store_id, sku, requested_units
            FROM shelfguard.daily_demand_v3
            WHERE demand_date BETWEEN %s AND %s
        """, (start, day, start, day))
        demand = defaultdict(list)
        for observed, store, sku, units in cur.fetchall():
            demand[(store, sku)].append((observed, units))
        if set(demand) != set(stock) or any(
            len({d for d, _ in values}) != 28 for values in demand.values()
        ):
            raise ValueError("Each store-medicine pair needs 28 distinct demand days")

        leads = defaultdict(list)
        for po_id, receipt_day in received.items():
            po = orders.get(po_id)
            if po is None:
                raise ValueError(f"Unknown receipt PO: {po_id}")
            if receipt_day >= day - timedelta(days=89):
                lead = (receipt_day - date.fromisoformat(po["order_date"])).days
                if lead < 0:
                    raise ValueError(f"Receipt precedes order: {po_id}")
                leads[po["supplier_id"]].append(lead)

        supplier_stats = {}
        for supplier in {v[0] for v in products.values()}:
            history = leads[supplier]
            if len(history) < 5:
                raise ValueError(f"Too few completed receipts for {supplier}")
            supplier_stats[supplier] = (mean(history), std(history))

        open_orders = defaultdict(list)
        for po_id, po in orders.items():
            if po_id not in received:
                open_orders[(po["store_id"], po["sku"])].append(po)

        planned = {}
        for key in sorted(stock):
            store, sku = key
            supplier, pack, cost = products[sku]
            lead_mean, lead_std = supplier_stats[supplier]
            history = [units for _, units in demand[key]]
            demand_std = std(history)
            predicted = forecast[key]
            horizon = ceil(lead_mean) + 1  # One-day inventory review cycle.
            safety = ceil(1.65 * sqrt(
                lead_mean * demand_std ** 2
                + predicted ** 2 * lead_std ** 2
            ))
            target = ceil(predicted * (lead_mean + 1) + safety)
            reliable = overdue = later = 0
            for po in open_orders[key]:
                due = date.fromisoformat(po["expected_date"])
                units = int(po["ordered_units"])
                if due < day:
                    overdue += units
                elif due <= day + timedelta(days=horizon):
                    reliable += units
                else:
                    later += units
            needed = max(0, target - stock[key] - reliable)
            order_units = ceil(needed / pack) * pack
            estimate = Decimal(order_units) * cost
            planned[key] = (
                day + timedelta(days=1), supplier, predicted,
                stock[key], reliable, overdue, later,
                Decimal(str(round(lead_mean, 2))),
                Decimal(str(round(lead_std, 2))),
                Decimal(str(round(demand_std, 2))),
                safety, target, order_units, estimate,
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS shelfguard.order_recommendations_v3 (
                as_of_date DATE NOT NULL,
                store_id TEXT NOT NULL REFERENCES shelfguard.stores(store_id),
                sku TEXT NOT NULL REFERENCES shelfguard.products(sku),
                method TEXT NOT NULL,
                target_date DATE NOT NULL,
                supplier_id TEXT NOT NULL REFERENCES shelfguard.suppliers(supplier_id),
                forecast_units INTEGER NOT NULL,
                closing_units INTEGER NOT NULL,
                reliable_on_order_units INTEGER NOT NULL,
                overdue_units INTEGER NOT NULL,
                later_on_order_units INTEGER NOT NULL,
                mean_lead_days NUMERIC(7,2) NOT NULL,
                lead_std_days NUMERIC(7,2) NOT NULL,
                demand_std_units NUMERIC(9,2) NOT NULL,
                safety_stock_units INTEGER NOT NULL,
                target_stock_units INTEGER NOT NULL,
                recommended_units INTEGER NOT NULL CHECK (recommended_units >= 0),
                estimated_cost_usd NUMERIC(14,2) NOT NULL,
                generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (as_of_date, store_id, sku, method)
            )
        """)
        cur.executemany("""
            INSERT INTO shelfguard.order_recommendations_v3
                (as_of_date, store_id, sku, method, target_date,
                 supplier_id, forecast_units, closing_units,
                 reliable_on_order_units, overdue_units, later_on_order_units,
                 mean_lead_days, lead_std_days, demand_std_units,
                 safety_stock_units, target_stock_units,
                 recommended_units, estimated_cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (as_of_date, store_id, sku, method) DO NOTHING
        """, [
            (day, store, sku, METHOD, *values)
            for (store, sku), values in planned.items()
        ])
        cur.execute("""
            SELECT store_id, sku, target_date, supplier_id,
                   forecast_units, closing_units, reliable_on_order_units,
                   overdue_units, later_on_order_units, mean_lead_days,
                   lead_std_days, demand_std_units, safety_stock_units,
                   target_stock_units, recommended_units, estimated_cost_usd
            FROM shelfguard.order_recommendations_v3
            WHERE as_of_date = %s AND method = %s
        """, (day, METHOD))
        saved = {
            (store, sku): tuple(values)
            for store, sku, *values in cur.fetchall()
        }
        if saved != planned:
            raise ValueError("Recommendations changed on rerun; rolled back")

    count = sum(v[-2] > 0 for v in planned.values())
    units = sum(v[-2] for v in planned.values())
    overdue_pairs = sum(v[5] > 0 for v in planned.values())
    print(
        f"PASS: {day_text}: {len(planned):,} decisions; "
        f"{count} recommended PO lines ({units:,} units); "
        f"{overdue_pairs} store-medicine pairs have overdue supply. "
        "Recommendations only; no purchase order was placed."
    )
    return day_text
