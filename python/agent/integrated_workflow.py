"""One read-only LangGraph investigation: brief + donor evidence."""

from __future__ import annotations

from getpass import getpass
from typing import TypedDict
import json

import psycopg
from langgraph.graph import START, END, StateGraph

from donor_evidence import find_donors
from workflow import agent as brief_agent


class InvestigationState(TypedDict, total=False):
    business_day: str
    store_id: str
    sku: str
    password: str
    brief: dict
    donors: dict
    final: dict


def investigate_exception(state: InvestigationState) -> dict:
    print("1/3  Investigating shortage and supplier orders...")

    result = brief_agent.invoke(
        {
            "business_day": state["business_day"],
            "store_id": state["store_id"],
            "sku": state["sku"],
            "password": state["password"],
        }
    )["result"]

    return {"brief": result}


def check_donors(state: InvestigationState) -> dict:
    print("2/3  Checking donor stock with a three-day reserve...")

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="pharmacy_shelfguard_fresh",
        user="postgres",
        password=state["password"],
        options="-c default_transaction_read_only=on",
        connect_timeout=5,
    ) as connection:
        donors = find_donors(
            connection,
            state["business_day"],
            state["store_id"],
            state["sku"],
            reserve_days=3,
        )

    return {"donors": donors}


def reconcile_case(state: InvestigationState) -> dict:
    print("3/3  Reconciling both sources and preparing the handoff...")

    brief = state["brief"]
    donors = state["donors"]
    case = brief["case"]

    if (
        case["business_day"] != donors["business_day"]
        or case["store_id"] != donors["requesting_store"]
        or case["sku"] != donors["sku"]
        or case["unmet_units"] != donors["unmet_units"]
    ):
        raise ValueError(
            "The brief and donor search describe different cases."
        )

    # Prioritize a same-region candidate when one exists.
    same_region = [
        row
        for row in donors["top_candidates"]
        if row["same_region"]
    ]
    candidate = (
        same_region[0]
        if same_region
        else (
            donors["top_candidates"][0]
            if donors["top_candidates"]
            else None
        )
    )

    if candidate and case["unmet_units"] > 0:
        transfer_review = {
            "status": "CANDIDATE_FOR_HUMAN_REVIEW",
            "donor_store": candidate["donor_store"],
            "same_region": candidate["same_region"],
            "donor_closing_units": candidate["closing_units"],
            "protected_reserve_units": candidate["reserve_units"],
            "potential_spare_units": candidate["potential_spare_units"],
            "maximum_units_to_investigate": min(
                case["unmet_units"],
                candidate["potential_spare_units"],
            ),
            "next_checks": [
                "Confirm physical stock and other commitments",
                "Confirm transport time and handling requirements",
                "Obtain human approval before any transfer",
            ],
        }
    else:
        transfer_review = {
            "status": "NO_CANDIDATE_UNDER_RESERVE_RULE",
            "donor_reserve_days": donors["donor_reserve_days"],
        }

    final = {
        "business_day": case["business_day"],
        "store_id": case["store_id"],
        "sku": case["sku"],
        "unmet_units": case["unmet_units"],
        "overdue_po_count": case["overdue_po_count"],
        "brief_status": brief["status"],
        "supplier_or_review_action": brief["suggested_action"],
        "operating_rules": brief["rule_ids"],
        "possible_donor_stores": donors["candidate_count"],
        "transfer_review": transfer_review,
        "purchase_order_placed": False,
        "transfer_executed": False,
    }
    return {"final": final}


builder = StateGraph(InvestigationState)
builder.add_node("investigate_exception", investigate_exception)
builder.add_node("check_donors", check_donors)
builder.add_node("reconcile_case", reconcile_case)

builder.add_edge(START, "investigate_exception")
builder.add_edge("investigate_exception", "check_donors")
builder.add_edge("check_donors", "reconcile_case")
builder.add_edge("reconcile_case", END)

integrated_agent = builder.compile()


if __name__ == "__main__":
    password = getpass("PostgreSQL password: ")

    output = integrated_agent.invoke(
        {
            "business_day": "2026-05-07",
            "store_id": "ST036",
            "sku": "MED026",
            "password": password,
        }
    )
    print("\nINTEGRATED CASE HANDOFF")
    print(json.dumps(output["final"], indent=2))