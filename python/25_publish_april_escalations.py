from pathlib import Path
from datetime import date
from decimal import Decimal
from getpass import getpass
import csv
import json
import os

root = Path(__file__).resolve().parents[1]
source = root / "reports" / "23_escalation_decisions.csv"
report_path = (
    root / "reports" / "23_no_hindsight_sensitivity.json"
)

try:
    import psycopg
except ImportError:
    try:
        import psycopg2 as psycopg
    except ImportError:
        raise SystemExit(
            "PostgreSQL driver missing. Run: "
            "python -m pip install psycopg2-binary"
        )

report = json.loads(report_path.read_text(encoding="utf-8"))

with source.open(newline="", encoding="utf-8") as file:
    audit = list(csv.DictReader(file))

april = [
    row for row in audit
    if date(2026, 4, 1)
    <= date.fromisoformat(row["decision_date"])
    <= date(2026, 4, 30)
]

assert len(april) == report["april_orders_escalated"]
assert len({row["po_id"] for row in april}) == len(april)

records = []
for row in april:
    cost = Decimal(row["assumed_escalation_cost_usd"])
    assert cost == Decimal("25")

    records.append((
        row["po_id"],
        row["store_id"],
        row["sku"],
        date.fromisoformat(row["decision_date"]),
        int(row["historical_unmet_units"]),
        date.fromisoformat(row["original_receipt_date"]),
        date.fromisoformat(row["policy_receipt_date"]),
        int(row["arrival_changed"]),
        cost,
    ))

assert sum(record[-1] for record in records) == Decimal(
    str(report["estimated_april_escalation_cost_usd"])
)

password = os.getenv("PGPASSWORD") or getpass(
    "Password for postgres on localhost:5433: "
)

connection = psycopg.connect(
    host="localhost",
    port=5433,
    dbname="pharmacy_shelfguard_fresh",
    user="postgres",
    password=password,
    connect_timeout=10,
)

try:
    with connection:
        with connection.cursor() as cur:
            cur.execute("SELECT current_database()")
            database = cur.fetchone()[0]
            assert database == "pharmacy_shelfguard_fresh"

            cur.execute("""
                CREATE TABLE IF NOT EXISTS
                    shelfguard.april_escalation_decisions (
                    po_id TEXT PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    decision_date DATE NOT NULL,
                    historical_unmet_units INTEGER NOT NULL,
                    original_receipt_date DATE NOT NULL,
                    policy_receipt_date DATE NOT NULL,
                    arrival_changed INTEGER NOT NULL,
                    assumed_escalation_cost_usd
                        NUMERIC(12, 2) NOT NULL
                )
            """)

            # Replace only this April decision table on re-run.
            # A failed load rolls the transaction back.
            cur.execute(
                "DELETE FROM "
                "shelfguard.april_escalation_decisions"
            )

            cur.executemany("""
                INSERT INTO
                    shelfguard.april_escalation_decisions (
                    po_id,
                    store_id,
                    sku,
                    decision_date,
                    historical_unmet_units,
                    original_receipt_date,
                    policy_receipt_date,
                    arrival_changed,
                    assumed_escalation_cost_usd
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, records)

            cur.execute("""
                SELECT
                    COUNT(*),
                    COALESCE(
                        SUM(assumed_escalation_cost_usd), 0
                    ),
                    COALESCE(SUM(arrival_changed), 0),
                    COUNT(DISTINCT store_id)
                FROM shelfguard.april_escalation_decisions
            """)
            (
                order_count,
                total_cost,
                arrivals_changed,
                stores_with_escalations,
            ) = cur.fetchone()

            assert order_count == (
                report["april_orders_escalated"]
            )
            assert total_cost == Decimal(
                str(
                    report[
                        "estimated_april_escalation_cost_usd"
                    ]
                )
            )

finally:
    connection.close()

result = {
    "database": database,
    "table": "shelfguard.april_escalation_decisions",
    "april_orders_escalated": order_count,
    "estimated_cost_usd": float(total_cost),
    "orders_with_earlier_arrival": arrivals_changed,
    "stores_with_escalations": stores_with_escalations,
    "validation": "Passed: PostgreSQL matches Step 23",
    "cost_assumption": (
        "$25 per April escalation, including orders "
        "that would have arrived the next day anyway"
    ),
}

(root / "reports" / "25_publish_april_escalations.json").write_text(
    json.dumps(result, indent=2),
    encoding="utf-8"
)
print(json.dumps(result, indent=2))