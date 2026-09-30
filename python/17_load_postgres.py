from pathlib import Path
from getpass import getpass
import json

import psycopg

root = Path(__file__).resolve().parents[1]
raw = root / "data" / "raw"
processed = root / "data" / "processed"
reports = root / "reports"

# Order matters: referenced suppliers and stores load first.
files = [
    ("suppliers", raw / "suppliers.csv",
     "supplier_id, supplier_name"),
    ("stores", raw / "stores.csv",
     "store_id, region"),
    ("products", raw / "products.csv",
     "sku, product_name, category, supplier_id, "
     "unit_cost_usd, case_pack, base_daily_demand"),
    ("customer_demand", raw / "customer_demand.csv",
     "demand_date, store_id, sku, requested_units"),
    ("purchase_orders",
     processed / "baseline_purchase_orders.csv",
     "po_id, order_date, expected_date, actual_receipt_date, "
     "store_id, sku, supplier_id, ordered_units"),
    ("goods_receipts",
     processed / "baseline_goods_receipts.csv",
     "receipt_id, po_id, receipt_date, store_id, sku, "
     "accepted_units"),
    ("baseline_daily", processed / "baseline_daily.csv",
     "inventory_date, store_id, sku, opening_units, "
     "received_units, requested_units, fulfilled_units, "
     "unmet_units, ordered_units, closing_units"),
]

missing = [str(path) for _, path, _ in files if not path.exists()]
if missing:
    raise SystemExit(f"Missing source file(s): {missing}")

password = getpass("PostgreSQL postgres-user password: ")

# The transaction commits only if every file and count succeeds.
with psycopg.connect(
    host="localhost",
    port=5433,
    dbname="pharmacy_shelfguard_fresh",
    user="postgres",
    password=password,
) as conn:
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database = cursor.fetchone()[0]
        if database != "pharmacy_shelfguard_fresh":
            raise SystemExit(f"Wrong database: {database}")

        cursor.execute(
            "SELECT to_regclass('shelfguard.customer_demand')"
        )
        if cursor.fetchone()[0] is None:
            raise SystemExit(
                "ShelfGuard tables are missing. Run "
                "sql/01_create_tables.sql in the fresh database first."
            )

        # Prevent an accidental second import.
        for table, _, _ in files:
            cursor.execute(
                f"SELECT count(*) FROM shelfguard.{table}"
            )
            if cursor.fetchone()[0] != 0:
                raise SystemExit(
                    f"shelfguard.{table} already has rows. "
                    "No files were loaded."
                )

        for table, path, columns in files:
            statement = (
                f"COPY shelfguard.{table} ({columns}) "
                "FROM STDIN WITH (FORMAT CSV, HEADER TRUE)"
            )
            with cursor.copy(statement) as copy:
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        copy.write(block)

        counts = {}
        for table, _, _ in files:
            cursor.execute(
                f"SELECT count(*) FROM shelfguard.{table}"
            )
            counts[table] = cursor.fetchone()[0]

        assert counts["stores"] == 40
        assert counts["suppliers"] == 4
        assert counts["products"] == 30
        assert counts["customer_demand"] == 72000
        assert counts["baseline_daily"] == 72000

        cursor.execute("""
            SELECT count(*)
            FROM shelfguard.goods_receipts gr
            JOIN shelfguard.purchase_orders po
              ON gr.po_id = po.po_id
            WHERE gr.store_id <> po.store_id
               OR gr.sku <> po.sku
               OR gr.accepted_units > po.ordered_units
        """)
        mismatched_receipts = cursor.fetchone()[0]
        assert mismatched_receipts == 0

# Exiting the connection block commits the successful load.
result = {
    "database": "pharmacy_shelfguard_fresh",
    "schema": "shelfguard",
    "row_counts": counts,
    "receipt_document_mismatches": mismatched_receipts,
    "validation": "Passed",
}
(reports / "17_database_load.json").write_text(
    json.dumps(result, indent=2), encoding="utf-8"
)
print(json.dumps(result, indent=2))
print("\nSaved verification: reports/17_database_load.json")