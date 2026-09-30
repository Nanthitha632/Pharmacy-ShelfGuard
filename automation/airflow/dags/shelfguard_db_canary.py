"""Daily read-only health and KPI check for the ShelfGuard database."""

from datetime import timedelta
from pathlib import Path
import json

import pendulum
from airflow.sdk import dag, task

REPORT = Path("/opt/shelfguard/reports/23_no_hindsight_sensitivity.json")
ORIGINAL = Path("/opt/shelfguard/reports/22_april_final_evaluation.json")


@dag(
    dag_id="shelfguard_db_canary",
    start_date=pendulum.datetime(2026, 9, 28, tz="America/Chicago"),
    schedule="0 8 * * *",
    catchup=False,
    tags=["shelfguard", "database", "monitoring"],
    description="Check PostgreSQL connectivity and reconcile published April KPIs.",
)
def shelfguard_db_canary():

    @task(retries=2, retry_delay=timedelta(minutes=2))
    def check_database():
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")
        with hook.get_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT current_database(),
                           to_regclass('shelfguard.april_policy_comparison')
                """)
                database, table = cursor.fetchone()

        assert database == "pharmacy_shelfguard_fresh", database
        assert table is not None, "Published April table is missing"
        print(f"PASS: connected to {database}; April table exists.")
        return database

    @task
    def reconcile_published_april(database):
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        assert REPORT.is_file() and ORIGINAL.is_file()
        expected = json.loads(REPORT.read_text(encoding="utf-8"))["april"]
        original = json.loads(ORIGINAL.read_text(encoding="utf-8"))["april"]

        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")
        with hook.get_conn() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*),
                           SUM(requested_units),
                           SUM(baseline_fulfilled_units),
                           SUM(policy_fulfilled_units),
                           COUNT(*) FILTER (WHERE baseline_unmet_units > 0),
                           COUNT(*) FILTER (WHERE policy_unmet_units > 0)
                    FROM shelfguard.april_policy_comparison
                """)
                rows, requested, baseline, policy, base_days, policy_days = (
                    cursor.fetchone()
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM shelfguard.v_april_policy_daily"
                )
                daily_rows = cursor.fetchone()[0]

        assert rows == 36000 and daily_rows == 30
        assert requested == original["requested_units"]
        assert base_days == expected["baseline_stockout_store_sku_days"]
        assert policy_days == expected["policy_stockout_store_sku_days"]
        assert round(100 * baseline / requested, 2) == (
            expected["baseline_fill_rate_percent"]
        )
        assert round(100 * policy / requested, 2) == (
            expected["policy_fill_rate_percent"]
        )

        print(
            f"PASS: {database}: {rows:,} April rows, {daily_rows} daily "
            f"aggregates, fill rate {100 * baseline / requested:.2f}% → "
            f"{100 * policy / requested:.2f}%, stockout days "
            f"{base_days:,} → {policy_days:,}."
        )

    reconcile_published_april(check_database())


shelfguard_db_canary()
