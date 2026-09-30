"""Generate, validate, and store one synthetic demand day per Airflow interval."""

from contextlib import closing
from datetime import date, timedelta
from hashlib import sha256
import random

import pendulum
from airflow.sdk import dag, get_current_context, task

ANCHOR_RUN_DAY = date(2026, 9, 28)
FIRST_BUSINESS_DAY = date(2026, 5, 1)


@dag(
    dag_id="shelfguard_daily_demand",
    start_date=pendulum.datetime(2026, 9, 27, 8, tz="America/Chicago"),
    schedule="0 8 * * *",
    catchup=True,
    params={"business_day": ""},
    max_active_runs=1,
    tags=["shelfguard", "demand", "synthetic"],
    description="Validated, repeatable daily synthetic demand intake.",
)
def shelfguard_daily_demand():

    @task
    def choose_business_day():
        context = get_current_context()
        override = context["params"]["business_day"].strip()

        if override:
            business_day = date.fromisoformat(override)
            if not date(2026, 5, 1) <= business_day <= date(2026, 5, 31):
                raise ValueError("Manual business day must be in May 2026")
            print(f"Manual simulation day: {business_day}.")
        else:
            interval_end = context["data_interval_end"]
            run_day = interval_end.in_timezone("America/Chicago").date()
            offset = (run_day - ANCHOR_RUN_DAY).days
            assert offset >= 0, (
                f"Run is before the simulation anchor: {run_day}"
            )
            business_day = FIRST_BUSINESS_DAY + timedelta(days=offset)
            print(
                f"Airflow interval ends {run_day}; "
                f"simulated day is {business_day}."
            )

        return business_day.isoformat()

    @task
    def load_demand(day_text):
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        business_day = date.fromisoformat(day_text)
        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")

        with closing(hook.get_conn()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(348725, 1)"
                    )
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS
                            shelfguard.daily_demand_v3 (
                            demand_date DATE NOT NULL,
                            store_id TEXT NOT NULL
                                REFERENCES shelfguard.stores(store_id),
                            sku TEXT NOT NULL
                                REFERENCES shelfguard.products(sku),
                            requested_units INTEGER NOT NULL
                                CHECK (requested_units >= 0),
                            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (demand_date, store_id, sku)
                        )
                    """)
                    cursor.execute(
                        "SELECT MAX(demand_date) "
                        "FROM shelfguard.daily_demand_v3"
                    )
                    latest = cursor.fetchone()[0]
                    if latest is None:
                        assert business_day == FIRST_BUSINESS_DAY, (
                            "First load must be May 1, 2026"
                        )
                    elif business_day > latest + timedelta(days=1):
                        raise ValueError(
                            f"Missing simulated day after {latest}; "
                            f"cannot load {business_day}"
                        )

                    cursor.execute(
                        "SELECT store_id FROM shelfguard.stores ORDER BY store_id"
                    )
                    stores = [row[0] for row in cursor.fetchall()]
                    cursor.execute("""
                        SELECT sku, base_daily_demand
                        FROM shelfguard.products
                        ORDER BY sku
                    """)
                    products = cursor.fetchall()
                    assert len(stores) == 40 and len(products) == 30

                    planned = {}
                    for store in stores:
                        store_number = int(store[-3:])
                        store_factor = 0.75 + (store_number % 7) * 0.09
                        weekend = 1.12 if business_day.weekday() >= 5 else 1.0

                        for sku, base in products:
                            seed = int.from_bytes(
                                sha256(
                                    f"v3|{day_text}|{store}|{sku}".encode()
                                ).digest()[:8],
                                "big",
                            )
                            rng = random.Random(seed)
                            spike = 1.8 if rng.random() < 0.015 else 1.0
                            typical = base * store_factor * weekend * spike
                            units = max(0, round(rng.gauss(typical, 2.2)))
                            planned[(store, sku)] = units

                    assert len(planned) == 1200
                    cursor.executemany("""
                        INSERT INTO shelfguard.daily_demand_v3
                            (demand_date, store_id, sku, requested_units)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (demand_date, store_id, sku) DO NOTHING
                    """, [
                        (business_day, store, sku, units)
                        for (store, sku), units in planned.items()
                    ])

                    cursor.execute("""
                        SELECT store_id, sku, requested_units
                        FROM shelfguard.daily_demand_v3
                        WHERE demand_date = %s
                    """, (business_day,))
                    saved = {
                        (store, sku): units
                        for store, sku, units in cursor.fetchall()
                    }
                    if saved != planned:
                        raise ValueError(
                            "Saved demand differs from deterministic source; "
                            "transaction rolled back"
                        )

        print(
            f"PASS: {day_text}: 1,200 unique store-medicine requests "
            f"saved or verified without duplicates."
        )
        return day_text

    @task
    def confirm_daily_total(day_text):
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")
        with closing(hook.get_conn()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*), SUM(requested_units)
                    FROM shelfguard.daily_demand_v3
                    WHERE demand_date = %s
                """, (day_text,))
                rows, units = cursor.fetchone()

        assert rows == 1200 and units is not None
        print(
            f"PASS: simulated {day_text}: {rows:,} records; "
            f"{units:,} customer units requested."
        )

    @task
    def settle_inventory(day_text, completed_check):
        """Post today's receipts and balance store inventory."""
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        import sys

        sys.path.insert(0, "/opt/shelfguard/python")
        from daily_inventory_v3 import settle

        connection = PostgresHook(
            postgres_conn_id="shelfguard_postgres"
        ).get_conn()
        return settle(day_text, connection)

    @task
    def forecast_next_day(day_text, completed_check):
        """Forecast the next day using only requests known by day_text."""
        from collections import defaultdict
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        as_of = date.fromisoformat(day_text)
        target = as_of + timedelta(days=1)
        history_start = as_of - timedelta(days=27)
        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")
        history = defaultdict(list)

        with closing(hook.get_conn()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT date, store_id, sku, requested_units
                        FROM shelfguard.april_policy_comparison
                        WHERE date BETWEEN %s AND %s
                        UNION ALL
                        SELECT demand_date, store_id, sku, requested_units
                        FROM shelfguard.daily_demand_v3
                        WHERE demand_date BETWEEN %s AND %s
                        ORDER BY 1, 2, 3
                    """, (
                        history_start, as_of,
                        history_start, as_of,
                    ))
                    for observed_day, store, sku, units in cursor.fetchall():
                        history[(store, sku)].append(
                            (observed_day, units)
                        )

                    if len(history) != 1200:
                        raise ValueError(
                            f"Expected 1,200 store-medicine histories; "
                            f"found {len(history)}"
                        )

                    planned = {}
                    for key, observations in history.items():
                        dates = {day for day, _ in observations}
                        weekdays = [
                            units for day, units in observations
                            if day.weekday() == target.weekday()
                        ]
                        if len(dates) != 28 or len(weekdays) != 4:
                            raise ValueError(
                                f"Incomplete 28-day history for {key}"
                            )

                        overall_mean = sum(
                            units for _, units in observations
                        ) / 28
                        weekday_mean = sum(weekdays) / 4
                        planned[key] = max(
                            0,
                            round(
                                0.65 * weekday_mean
                                + 0.35 * overall_mean
                            ),
                        )

                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS
                            shelfguard.daily_forecast_v3 (
                            as_of_date DATE NOT NULL,
                            target_date DATE NOT NULL,
                            store_id TEXT NOT NULL
                                REFERENCES shelfguard.stores(store_id),
                            sku TEXT NOT NULL
                                REFERENCES shelfguard.products(sku),
                            method TEXT NOT NULL,
                            forecast_units INTEGER NOT NULL
                                CHECK (forecast_units >= 0),
                            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY
                                (as_of_date, store_id, sku, method),
                            CHECK (target_date > as_of_date)
                        )
                    """)

                    method = "weekday_blend_28d_v1"
                    cursor.executemany("""
                        INSERT INTO shelfguard.daily_forecast_v3
                            (as_of_date, target_date, store_id, sku,
                             method, forecast_units)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT
                            (as_of_date, store_id, sku, method)
                        DO NOTHING
                    """, [
                        (as_of, target, store, sku, method, units)
                        for (store, sku), units in sorted(planned.items())
                    ])

                    cursor.execute("""
                        SELECT store_id, sku, target_date, forecast_units
                        FROM shelfguard.daily_forecast_v3
                        WHERE as_of_date = %s AND method = %s
                    """, (as_of, method))
                    saved = {
                        (store, sku): (saved_target, units)
                        for store, sku, saved_target, units
                        in cursor.fetchall()
                    }
                    expected = {
                        key: (target, units)
                        for key, units in planned.items()
                    }
                    if saved != expected:
                        raise ValueError(
                            "Saved forecast differs on rerun; "
                            "transaction rolled back"
                        )

        print(
            f"PASS: {day_text} → {target}: "
            f"{len(planned):,} forecasts, "
            f"{sum(planned.values()):,} predicted units. "
            "No future actual demand was used."
        )

    @task
    def recommend_purchase_orders(day_text, forecast_done):
        """Save planning suggestions; do not place purchase orders."""
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        import sys

        sys.path.insert(0, "/opt/shelfguard/python")
        from recommend_orders_v3 import recommend

        connection = PostgresHook(
            postgres_conn_id="shelfguard_postgres"
        ).get_conn()
        return recommend(day_text, connection)

    @task
    def assess_procurement_budget(day_text):
        """Record the proposed spend for buyer review; place no POs."""
        from contextlib import closing
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        hook = PostgresHook(postgres_conn_id="shelfguard_postgres")
        with closing(hook.get_conn()) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT
                            COUNT(*),
                            COUNT(*) FILTER (
                                WHERE recommended_units > 0
                            ),
                            COALESCE(SUM(recommended_units), 0),
                            COALESCE(SUM(estimated_cost_usd), 0),
                            COUNT(*) FILTER (
                                WHERE overdue_units > 0
                            )
                        FROM shelfguard.order_recommendations_v3
                        WHERE as_of_date = %s
                          AND method = 'lead_time_safety_stock_v1'
                    """, (day_text,))
                    pairs, lines, units, spend, overdue = cursor.fetchone()

                    if pairs != 1200 or lines == 0:
                        raise ValueError(
                            f"Incomplete purchase review: {pairs} pairs"
                        )

                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS
                            shelfguard.procurement_review_v3 (
                            as_of_date DATE PRIMARY KEY,
                            evaluated_pairs INTEGER NOT NULL,
                            proposed_po_lines INTEGER NOT NULL,
                            proposed_units INTEGER NOT NULL,
                            estimated_spend_usd NUMERIC(16,2) NOT NULL,
                            overdue_store_sku_pairs INTEGER NOT NULL,
                            review_status TEXT NOT NULL
                                CHECK (
                                    review_status =
                                    'PENDING_BUDGET_REVIEW'
                                ),
                            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO shelfguard.procurement_review_v3
                            (as_of_date, evaluated_pairs,
                             proposed_po_lines, proposed_units,
                             estimated_spend_usd,
                             overdue_store_sku_pairs, review_status)
                        VALUES (%s, %s, %s, %s, %s, %s,
                                'PENDING_BUDGET_REVIEW')
                        ON CONFLICT (as_of_date) DO NOTHING
                    """, (
                        day_text, pairs, lines, units, spend, overdue
                    ))
                    cursor.execute("""
                        SELECT evaluated_pairs, proposed_po_lines,
                               proposed_units, estimated_spend_usd,
                               overdue_store_sku_pairs, review_status
                        FROM shelfguard.procurement_review_v3
                        WHERE as_of_date = %s
                    """, (day_text,))
                    saved = cursor.fetchone()
                    expected = (
                        pairs, lines, units, spend, overdue,
                        "PENDING_BUDGET_REVIEW"
                    )
                    if saved != expected:
                        raise ValueError(
                            "Review changed on rerun; transaction rolled back"
                        )

                    cursor.execute("""
                        SELECT supplier_id,
                               SUM(estimated_cost_usd) AS supplier_spend
                        FROM shelfguard.order_recommendations_v3
                        WHERE as_of_date = %s
                          AND recommended_units > 0
                        GROUP BY supplier_id
                        ORDER BY supplier_spend DESC
                        LIMIT 3
                    """, (day_text,))
                    top_suppliers = cursor.fetchall()

        print(
            f"PASS: {day_text}: {lines} proposed PO lines; "
            f"{units:,} units; estimated spend ${spend:,.2f}; "
            f"{overdue} overdue store-medicine pairs. "
            "Status: PENDING_BUDGET_REVIEW. No POs placed."
        )
        print(f"Top three suppliers by proposed spend: {top_suppliers}")
        return day_text

    @task
    def triage_purchase_proposals(day_text):
        """Build the buyer worklist; approve and place no POs."""
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        import sys

        sys.path.insert(0, "/opt/shelfguard/python")
        from approval_queue_v3 import build

        connection = PostgresHook(
            postgres_conn_id="shelfguard_postgres"
        ).get_conn()
        return build(day_text, connection)

    @task
    def issue_simulated_purchase_orders(day_text):
        """Create synthetic PO documents under the scenario approval rule."""
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        import sys

        sys.path.insert(0, "/opt/shelfguard/python")
        from issue_pos_v3 import issue

        connection = PostgresHook(
            postgres_conn_id="shelfguard_postgres"
        ).get_conn()
        return issue(day_text, connection)

    @task
    def monitor_exceptions(day_text, inventory_done):
        """Record daily stockouts and overdue new May purchase orders."""
        from contextlib import closing
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        connection = PostgresHook(
            postgres_conn_id="shelfguard_postgres"
        ).get_conn()

        with closing(connection) as conn, conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS
                    shelfguard.daily_exception_summary_v3 (
                    business_day DATE NOT NULL,
                    action TEXT NOT NULL,
                    store_medicine_pairs INTEGER NOT NULL,
                    unmet_units BIGINT NOT NULL,
                    overdue_purchase_orders BIGINT NOT NULL,
                    overdue_order_units BIGINT NOT NULL,
                    PRIMARY KEY (business_day, action)
                )
            """)

            cur.execute("""
                SELECT COUNT(*)
                FROM shelfguard.daily_inventory_v3
                WHERE inventory_date = %s
            """, (day_text,))
            if cur.fetchone()[0] != 1200:
                raise ValueError("Inventory day is incomplete")

            cur.execute("""
                WITH overdue AS (
                    SELECT
                        p.store_id,
                        p.sku,
                        COUNT(*) AS po_count,
                        SUM(p.ordered_units) AS units
                    FROM shelfguard.purchase_orders_v3 AS p
                    LEFT JOIN shelfguard.goods_receipts_v3 AS g
                        ON g.po_id = p.po_id
                       AND g.receipt_date <= %s
                    WHERE p.order_date < %s
                      AND p.expected_date < %s
                      AND g.po_id IS NULL
                    GROUP BY p.store_id, p.sku
                ),
                classified AS (
                    SELECT
                        CASE
                            WHEN i.unmet_units > 0
                             AND COALESCE(o.po_count, 0) > 0
                                THEN 'Critical: stockout and overdue PO'
                            WHEN i.unmet_units > 0
                                THEN 'Stockout: investigate supply'
                            ELSE 'Supplier follow-up'
                        END AS action,
                        i.unmet_units,
                        COALESCE(o.po_count, 0) AS po_count,
                        COALESCE(o.units, 0) AS overdue_units
                    FROM shelfguard.daily_inventory_v3 AS i
                    LEFT JOIN overdue AS o
                        ON o.store_id = i.store_id
                       AND o.sku = i.sku
                    WHERE i.inventory_date = %s
                      AND (
                          i.unmet_units > 0
                          OR COALESCE(o.po_count, 0) > 0
                      )
                )
                SELECT
                    action,
                    COUNT(*)::INTEGER,
                    SUM(unmet_units),
                    SUM(po_count),
                    SUM(overdue_units)
                FROM classified
                GROUP BY action
                ORDER BY action
            """, (day_text, day_text, day_text, day_text))
            planned = {
                action: (pairs, unmet, po_count, units)
                for action, pairs, unmet, po_count, units
                in cur.fetchall()
            }

            cur.executemany("""
                INSERT INTO shelfguard.daily_exception_summary_v3
                    (business_day, action, store_medicine_pairs,
                     unmet_units, overdue_purchase_orders,
                     overdue_order_units)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (business_day, action) DO NOTHING
            """, [
                (day_text, action, *values)
                for action, values in sorted(planned.items())
            ])

            cur.execute("""
                SELECT action, store_medicine_pairs, unmet_units,
                       overdue_purchase_orders, overdue_order_units
                FROM shelfguard.daily_exception_summary_v3
                WHERE business_day = %s
            """, (day_text,))
            saved = {
                action: (pairs, unmet, po_count, units)
                for action, pairs, unmet, po_count, units
                in cur.fetchall()
            }
            if saved != planned:
                raise ValueError(
                    "Saved exception report differs on rerun"
                )

        print(f"PASS: {day_text} exception report: {planned}")
        return day_text

    day = choose_business_day()
    saved = load_demand(day)
    checked = confirm_daily_total(saved)
    settled = settle_inventory(day, checked)
    monitor_exceptions(day, settled)
    forecast = forecast_next_day(day, settled)
    proposed = recommend_purchase_orders(day, forecast)
    reviewed = assess_procurement_budget(proposed)
    triaged = triage_purchase_proposals(reviewed)
    issue_simulated_purchase_orders(triaged)


shelfguard_daily_demand()

