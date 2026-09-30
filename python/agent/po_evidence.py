"""Investigate purchase orders as they stood on a case's business day."""

from getpass import getpass

import psycopg
from psycopg.rows import dict_row

from evidence import case_file, check_case_view, priority_cases


def purchase_order_timeline(connection, case):
    """Return this store-SKU's POs without looking at future receipts."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                p.po_id,
                p.order_date,
                p.expected_date,
                p.ordered_units,
                p.po_status,
                g.receipt_date,
                COALESCE(g.accepted_units, 0) AS received_by_case_day,
                GREATEST(
                    p.ordered_units - COALESCE(g.accepted_units, 0), 0
                ) AS open_units,
                (
                    p.expected_date < %s
                    AND p.ordered_units > COALESCE(g.accepted_units, 0)
                ) AS overdue_on_case_day
            FROM shelfguard.purchase_orders_v3 AS p
            LEFT JOIN shelfguard.goods_receipts_v3 AS g
                ON g.po_id = p.po_id
               AND g.receipt_date <= %s
            WHERE p.store_id = %s
              AND p.sku = %s
              AND p.order_date <= %s
            ORDER BY p.expected_date DESC, p.po_id
            """,
            (
                case["business_day"],
                case["business_day"],
                case["store_id"],
                case["sku"],
                case["business_day"],
            ),
        )
        return cursor.fetchall()


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
        check_case_view(connection)
        first = priority_cases(connection, limit=1)[0]
        case = case_file(connection, first["store_id"], first["sku"])
        orders = purchase_order_timeline(connection, case)

    overdue = [po for po in orders if po["overdue_on_case_day"]]
    overdue_units = sum(po["open_units"] for po in overdue)

    print(
        f"\nCASE: {case['business_day']} | "
        f"{case['store_id']} / {case['sku']}"
    )
    print(
        f"Exception view: {case['overdue_po_count']} overdue POs, "
        f"{case['overdue_units']} overdue units"
    )
    print(
        f"PO timeline:    {len(overdue)} overdue POs, "
        f"{overdue_units} open units"
    )

    for po in overdue:
        print(
            f"\n{po['po_id']} | ordered {po['order_date']} | "
            f"expected {po['expected_date']}"
        )
        print(
            f"ordered={po['ordered_units']}, "
            f"received by case day={po['received_by_case_day']}, "
            f"still open={po['open_units']}, status={po['po_status']}"
        )

    if (
        len(overdue) == case["overdue_po_count"]
        and overdue_units == case["overdue_units"]
    ):
        print("\nPASS: PO timeline agrees with the exception view.")
    else:
        print("\nREVIEW: PO timeline and exception view use different rules.")