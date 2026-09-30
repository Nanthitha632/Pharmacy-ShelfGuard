"""Read-only, as-of-day search for possible store-to-store donors."""

from __future__ import annotations

from getpass import getpass
import json

import psycopg
from psycopg.rows import dict_row


BUSINESS_DAY = "2026-05-07"
REQUESTING_STORE = "ST036"
SKU = "MED026"
DONOR_RESERVE_DAYS = 3


def find_donors(
    connection,
    business_day: str,
    requesting_store: str,
    sku: str,
    reserve_days: int = 3,
) -> dict:
    if reserve_days < 1:
        raise ValueError("Reserve days must be at least 1.")

    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT i.unmet_units, s.region
            FROM shelfguard.daily_inventory_v3 AS i
            JOIN shelfguard.stores AS s
              ON s.store_id = i.store_id
            WHERE i.inventory_date = %s
              AND i.store_id = %s
              AND i.sku = %s
            """,
            (business_day, requesting_store, sku),
        )
        target = cur.fetchone()
        if target is None:
            raise ValueError("Requesting store has no inventory snapshot.")

        cur.execute("SELECT COUNT(*) AS store_count FROM shelfguard.stores")
        expected_stores = cur.fetchone()["store_count"]

        cur.execute(
            """
            SELECT i.store_id,
                   s.region,
                   i.closing_units,
                   p.base_daily_demand
            FROM shelfguard.daily_inventory_v3 AS i
            JOIN shelfguard.stores AS s
              ON s.store_id = i.store_id
            JOIN shelfguard.products AS p
              ON p.sku = i.sku
            WHERE i.inventory_date = %s
              AND i.sku = %s
            ORDER BY i.store_id
            """,
            (business_day, sku),
        )
        snapshots = cur.fetchall()

    if len(snapshots) != expected_stores:
        raise ValueError(
            f"Incomplete {sku} snapshot: "
            f"{len(snapshots)} of {expected_stores} stores."
        )

    candidates = []
    for row in snapshots:
        if row["store_id"] == requesting_store:
            continue

        closing = int(row["closing_units"])
        daily_demand = int(row["base_daily_demand"])
        reserve = reserve_days * daily_demand
        spare = max(0, closing - reserve)

        if spare <= 0:
            continue

        candidates.append(
            {
                "donor_store": row["store_id"],
                "region": row["region"],
                "same_region": row["region"] == target["region"],
                "closing_units": closing,
                "reserve_units": reserve,
                "potential_spare_units": spare,
            }
        )

    candidates.sort(
        key=lambda row: (
            not row["same_region"],
            -row["potential_spare_units"],
            row["donor_store"],
        )
    )

    unmet = int(target["unmet_units"])
    return {
        "business_day": business_day,
        "requesting_store": requesting_store,
        "sku": sku,
        "requesting_region": target["region"],
        "unmet_units": unmet,
        "donor_reserve_days": reserve_days,
        "candidate_count": len(candidates),
        "top_candidates": candidates[:10],
        "interpretation": (
            "Potential spare stock after a simulated donor reserve. "
            "A transfer still requires checking stock freshness, "
            "other commitments, transport time, and human approval."
        ),
        "transfer_executed": False,
    }


if __name__ == "__main__":
    password = getpass("PostgreSQL password: ")

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="pharmacy_shelfguard_fresh",
        user="postgres",
        password=password,
        options="-c default_transaction_read_only=on",
        connect_timeout=5,
    ) as connection:
        result = find_donors(
            connection,
            BUSINESS_DAY,
            REQUESTING_STORE,
            SKU,
            DONOR_RESERVE_DAYS,
        )

    print(json.dumps(result, indent=2))