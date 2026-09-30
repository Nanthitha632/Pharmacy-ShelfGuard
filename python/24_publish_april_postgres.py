from pathlib import Path
from decimal import Decimal
from getpass import getpass
import csv
import json
import os

root = Path(__file__).resolve().parents[1]
source = (
    root / "data" / "v2" / "processed"
    / "policy_comparison_april_no_hindsight.csv"
)
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

with source.open(newline="", encoding="utf-8") as file:
    rows = list(csv.DictReader(file))

report = json.loads(report_path.read_text(encoding="utf-8"))
expected = report["april"]

assert report["validation"].startswith("Passed:")
assert len(rows) == 36000
assert len({
    (r["date"], r["store_id"], r["sku"]) for r in rows
}) == 36000
assert len({r["date"] for r in rows}) == 30

columns = [
    "date",
    "store_id",
    "sku",
    "requested_units",
    "baseline_fulfilled_units",
    "policy_fulfilled_units",
    "baseline_unmet_units",
    "policy_unmet_units",
    "baseline_closing_units",
    "policy_closing_units",
    "unit_cost_usd",
]

values = []
for r in rows:
    requested = int(r["requested_units"])
    base_filled = int(r["baseline_fulfilled_units"])
    policy_filled = int(r["policy_fulfilled_units"])
    base_unmet = int(r["baseline_unmet_units"])
    policy_unmet = int(r["policy_unmet_units"])

    assert requested == base_filled + base_unmet
    assert requested == policy_filled + policy_unmet
    assert r["date"].startswith("2026-04-")

    values.append((
        r["date"],
        r["store_id"],
        r["sku"],
        requested,
        base_filled,
        policy_filled,
        base_unmet,
        policy_unmet,
        int(r["baseline_closing_units"]),
        int(r["policy_closing_units"]),
        Decimal(r["unit_cost_usd"]),
    ))

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

            cur.execute(
                "CREATE SCHEMA IF NOT EXISTS shelfguard"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS
                    shelfguard.april_policy_comparison (
                    date DATE NOT NULL,
                    store_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    requested_units INTEGER NOT NULL,
                    baseline_fulfilled_units INTEGER NOT NULL,
                    policy_fulfilled_units INTEGER NOT NULL,
                    baseline_unmet_units INTEGER NOT NULL,
                    policy_unmet_units INTEGER NOT NULL,
                    baseline_closing_units INTEGER NOT NULL,
                    policy_closing_units INTEGER NOT NULL,
                    unit_cost_usd NUMERIC(12, 2) NOT NULL,
                    PRIMARY KEY (date, store_id, sku)
                )
            """)

            # Re-running this script updates the same April rows.
            # It does not create duplicate records.
            batch_size = 500
            for start in range(0, len(values), batch_size):
                batch = values[start:start + batch_size]
                one_row = "(" + ",".join(
                    ["%s"] * len(columns)
                ) + ")"
                placeholders = ",".join(
                    [one_row] * len(batch)
                )

                update = ",".join(
                    f"{col}=EXCLUDED.{col}"
                    for col in columns[3:]
                )
                sql = (
                    "INSERT INTO "
                    "shelfguard.april_policy_comparison "
                    f"({','.join(columns)}) VALUES "
                    f"{placeholders} "
                    "ON CONFLICT (date, store_id, sku) "
                    f"DO UPDATE SET {update}"
                )

                parameters = [
                    item
                    for record in batch
                    for item in record
                ]
                cur.execute(sql, parameters)

            cur.execute("""
                CREATE OR REPLACE VIEW
                    shelfguard.v_april_policy_daily AS
                SELECT
                    date,
                    SUM(requested_units) AS requested_units,
                    SUM(baseline_fulfilled_units)
                        AS baseline_fulfilled_units,
                    SUM(policy_fulfilled_units)
                        AS policy_fulfilled_units,
                    ROUND(
                        100.0 * SUM(baseline_fulfilled_units)
                        / NULLIF(SUM(requested_units), 0),
                        2
                    ) AS baseline_fill_rate_percent,
                    ROUND(
                        100.0 * SUM(policy_fulfilled_units)
                        / NULLIF(SUM(requested_units), 0),
                        2
                    ) AS policy_fill_rate_percent,
                    COUNT(*) FILTER (
                        WHERE baseline_unmet_units > 0
                    ) AS baseline_stockout_store_sku_days,
                    COUNT(*) FILTER (
                        WHERE policy_unmet_units > 0
                    ) AS policy_stockout_store_sku_days,
                    SUM(
                        baseline_closing_units * unit_cost_usd
                    ) AS baseline_inventory_value_usd,
                    SUM(
                        policy_closing_units * unit_cost_usd
                    ) AS policy_inventory_value_usd
                FROM shelfguard.april_policy_comparison
                GROUP BY date
            """)

            cur.execute("""
                SELECT
                    COUNT(*),
                    COUNT(DISTINCT date),
                    SUM(requested_units),
                    SUM(baseline_fulfilled_units),
                    SUM(policy_fulfilled_units),
                    COUNT(*) FILTER (
                        WHERE baseline_unmet_units > 0
                    ),
                    COUNT(*) FILTER (
                        WHERE policy_unmet_units > 0
                    ),
                    ROUND(
                        SUM(
                            baseline_closing_units
                            * unit_cost_usd
                        ) / 30,
                        2
                    ),
                    ROUND(
                        SUM(
                            policy_closing_units
                            * unit_cost_usd
                        ) / 30,
                        2
                    )
                FROM shelfguard.april_policy_comparison
            """)
            (
                count,
                days,
                requested,
                base_filled,
                policy_filled,
                base_stockouts,
                policy_stockouts,
                base_value,
                policy_value,
            ) = cur.fetchone()

            assert count == 36000
            assert days == 30
            assert base_stockouts == (
                expected["baseline_stockout_store_sku_days"]
            )
            assert policy_stockouts == (
                expected["policy_stockout_store_sku_days"]
            )
            assert round(
                100 * base_filled / requested, 2
            ) == expected["baseline_fill_rate_percent"]
            assert round(
                100 * policy_filled / requested, 2
            ) == expected["policy_fill_rate_percent"]
            assert abs(
                float(base_value) - expected[
                    "baseline_average_daily_inventory_value_usd"
                ]
            ) < 0.03
            assert abs(
                float(policy_value) - expected[
                    "policy_average_daily_inventory_value_usd"
                ]
            ) < 0.03

            cur.execute(
                "SELECT COUNT(*) "
                "FROM shelfguard.v_april_policy_daily"
            )
            assert cur.fetchone()[0] == 30

finally:
    connection.close()

confirmation = {
    "database": database,
    "source": str(source.relative_to(root)),
    "detail_table": "shelfguard.april_policy_comparison",
    "daily_view": "shelfguard.v_april_policy_daily",
    "april_detail_rows": count,
    "daily_view_rows": days,
    "validation": "Passed: PostgreSQL matches the Step 23 report",
}

output = root / "reports" / "24_postgres_publish.json"
output.write_text(
    json.dumps(confirmation, indent=2),
    encoding="utf-8"
)
print(json.dumps(confirmation, indent=2))