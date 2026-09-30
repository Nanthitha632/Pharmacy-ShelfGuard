"""Read-only, verified ShelfGuard case evidence."""

from getpass import getpass

import psycopg
from psycopg.rows import dict_row


def priority_cases(connection, limit=5):
    """Return the latest cases, putting stockout + overdue PO first."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT store_id, sku, product_name, region,
                   inventory_unmet_units, overdue_po_count,
                   overdue_units, action
            FROM shelfguard.v_agent_case_v3
            ORDER BY
                CASE
                    WHEN inventory_unmet_units > 0
                     AND overdue_po_count > 0 THEN 0
                    WHEN inventory_unmet_units > 0 THEN 1
                    ELSE 2
                END,
                inventory_unmet_units DESC,
                store_id, sku
            LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()


def case_file(connection, store_id, sku):
    """Fetch one case using safe parameters, never model-written SQL."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT business_day, store_id, region, sku, product_name,
                   supplier_id, unit_cost_usd, opening_units,
                   received_units, requested_units, fulfilled_units,
                   inventory_unmet_units, closing_units,
                   overdue_po_count, overdue_units, action,
                   facts_reconciled
            FROM shelfguard.v_agent_case_v3
            WHERE store_id = %s AND sku = %s
            """,
            (store_id, sku),
        )
        case = cursor.fetchone()

    if case is None:
        raise ValueError(f"No current case for {store_id} / {sku}")
    if not case["facts_reconciled"]:
        raise ValueError("Case failed inventory reconciliation")
    return case


def check_case_view(connection):
    """Fail before showing any case if the source needs review."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS cases_found,
                COUNT(*) FILTER (WHERE NOT facts_reconciled)
                    AS unreconciled,
                COUNT(*) FILTER (
                    WHERE product_name IS NULL OR region IS NULL
                ) AS missing_reference
            FROM shelfguard.v_agent_case_v3
            """
        )
        result = cursor.fetchone()

    if result["cases_found"] == 0:
        raise ValueError("No current exceptions found")
    if result["unreconciled"] or result["missing_reference"]:
        raise ValueError(f"Evidence needs review: {dict(result)}")
    return result["cases_found"]


if __name__ == "__main__":
    password = getpass("PostgreSQL postgres password: ")

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="pharmacy_shelfguard_fresh",
        user="postgres",
        password=password,
        connect_timeout=5,
        row_factory=dict_row,
    ) as connection:
        connection.read_only = True
        total = check_case_view(connection)
        cases = priority_cases(connection)
        focus = case_file(
            connection, cases[0]["store_id"], cases[0]["sku"]
        )

    print(f"\nPASS: {total} verified cases available")
    print("\nTOP 5 CASES")
    for case in cases:
        print(
            f"{case['store_id']} / {case['sku']} | "
            f"unmet={case['inventory_unmet_units']} | "
            f"overdue POs={case['overdue_po_count']} | "
            f"{case['action']}"
        )
    print("\nFOCUS CASE — FACTS FOR THE AGENT")
    for field, value in focus.items():
        print(f"{field}: {value}")