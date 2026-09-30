"""Read-only audit of the locked ShelfGuard April simulation."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import csv
import json

from airflow.sdk import dag, task

ROOT = Path("/opt/shelfguard")
COMPARISON = ROOT / "data/v2/processed/policy_comparison_april_no_hindsight.csv"
REPORT = ROOT / "reports/23_no_hindsight_sensitivity.json"
LOCK = ROOT / "docs/locked_policy_v2.json"


@dag(
    dag_id="shelfguard_april_audit",
    start_date=datetime(2026, 9, 28),
    schedule=None,
    catchup=False,
    tags=["shelfguard", "quality", "historical"],
    description="Verify the locked April simulation without changing source data.",
)
def shelfguard_april_audit():

    @task
    def check_sources():
        for path in (COMPARISON, REPORT, LOCK):
            if not path.is_file():
                raise FileNotFoundError(f"Required project file missing: {path}")

        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        assert lock["locked_after"] == "2026-03-31"
        assert lock["daily_escalation_limit"] == 50
        assert lock["final_evaluation"] == "2026-04-01 through 2026-04-30"

        print("PASS: April comparison, report, and March policy lock exist.")
        return {"daily_escalation_limit": 50}

    @task
    def reconcile_april(source_check):
        report = json.loads(REPORT.read_text(encoding="utf-8"))
        expected = report["april"]

        count = requested = base_filled = policy_filled = 0
        base_stockouts = policy_stockouts = 0
        base_value = policy_value = Decimal("0")
        keys = set()
        dates = set()

        with COMPARISON.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                day = row["date"]
                key = (day, row["store_id"], row["sku"])
                assert day.startswith("2026-04-"), f"Unexpected date: {day}"
                assert key not in keys, f"Duplicate store-SKU-day: {key}"
                keys.add(key)
                dates.add(day)
                count += 1

                req = int(row["requested_units"])
                bf = int(row["baseline_fulfilled_units"])
                pf = int(row["policy_fulfilled_units"])
                bu = int(row["baseline_unmet_units"])
                pu = int(row["policy_unmet_units"])
                bc = int(row["baseline_closing_units"])
                pc = int(row["policy_closing_units"])
                cost = Decimal(row["unit_cost_usd"])

                assert min(req, bf, pf, bu, pu, bc, pc) >= 0
                assert req == bf + bu == pf + pu

                requested += req
                base_filled += bf
                policy_filled += pf
                base_stockouts += bu > 0
                policy_stockouts += pu > 0
                base_value += bc * cost
                policy_value += pc * cost

        assert count == 36000 and len(dates) == 30
        original = json.loads(
            (ROOT / "reports/22_april_final_evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        assert requested == original["april"]["requested_units"]
        assert base_stockouts == expected["baseline_stockout_store_sku_days"]
        assert policy_stockouts == expected["policy_stockout_store_sku_days"]

        baseline_fill = round(100 * base_filled / requested, 2)
        policy_fill = round(100 * policy_filled / requested, 2)
        baseline_value = round(float(base_value / 30), 2)
        policy_value = round(float(policy_value / 30), 2)

        assert baseline_fill == expected["baseline_fill_rate_percent"]
        assert policy_fill == expected["policy_fill_rate_percent"]
        assert abs(
            baseline_value - expected["baseline_average_daily_inventory_value_usd"]
        ) <= 0.02
        assert abs(
            policy_value - expected["policy_average_daily_inventory_value_usd"]
        ) <= 0.02

        result = {
            "rows_verified": count,
            "policy_limit": source_check["daily_escalation_limit"],
            "baseline_fill_percent": baseline_fill,
            "policy_fill_percent": policy_fill,
            "baseline_stockout_days": base_stockouts,
            "policy_stockout_days": policy_stockouts,
            "baseline_avg_inventory_usd": baseline_value,
            "policy_avg_inventory_usd": policy_value,
        }
        print("PASS: April detail reconciles to the saved report.")
        print(json.dumps(result, indent=2))
        return result

    @task
    def record_decision(result):
        decision = (
            "HOLD"
            if result["policy_stockout_days"] > result["baseline_stockout_days"]
            else "REVIEW"
        )
        print(
            f"Decision: {decision}. Fill rate "
            f"{result['baseline_fill_percent']}% → "
            f"{result['policy_fill_percent']}%; stockout days "
            f"{result['baseline_stockout_days']} → "
            f"{result['policy_stockout_days']}%s".replace("%s", "")
        )
        print("This is a synthetic historical audit, not a new April test.")

    record_decision(reconcile_april(check_sources()))


shelfguard_april_audit()

