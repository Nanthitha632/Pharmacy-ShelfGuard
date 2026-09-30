"""Draft a grounded, read-only ShelfGuard exception brief."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path
from typing import Literal
import json
import re

import psycopg
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = ROOT / "docs" / "agent" / "shelfguard_operating_rules.md"
BUSINESS_DAY = "2026-05-07"
STORE_ID = "ST040"
SKU = "MED001"


class ProposedBrief(BaseModel):
    action: Literal[
        "ESCALATE_FOR_ETA", "CHECK_TRANSFER", "HOLD_FOR_REVIEW"
    ]
    reason: str = Field(description="One short reason supported by the evidence.")
    open_question: str = Field(
        description="One specific question for the human supply chain analyst."
    )


def load_rule() -> dict[str, str]:
    text = RULES_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^#{1,4}\s*.*?\[EXP-01\].*?$.*?"
        r"(?=^#{1,4}\s|\Z)",
        text,
    )
    if not match:
        raise ValueError("Cannot find the [EXP-01] rule in the rulebook.")
    return {"rule_id": "EXP-01", "text": match.group(0).strip()}


def main() -> None:
    password = getpass("PostgreSQL password: ")
    rule = load_rule()

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="pharmacy_shelfguard_fresh",
        user="postgres",
        password=password,
        options="-c default_transaction_read_only=on",
        row_factory=psycopg.rows.dict_row,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT business_day, store_id, sku, requested_units,
                       unmet_units, closing_units, overdue_po_count,
                       overdue_units, action
                FROM shelfguard.v_daily_exceptions_v3
                WHERE business_day = %s
                  AND store_id = %s
                  AND sku = %s
                """,
                (BUSINESS_DAY, STORE_ID, SKU),
            )
            case = cur.fetchone()
            if case is None:
                raise ValueError("The selected exception was not found.")

            cur.execute(
                """
                SELECT po_id, order_date, expected_date, ordered_units,
                       po_status
                FROM shelfguard.purchase_orders_v3
                WHERE store_id = %s
                  AND sku = %s
                  AND expected_date < %s
                  AND order_date <= %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM shelfguard.goods_receipts_v3 gr
                      WHERE gr.po_id = purchase_orders_v3.po_id
                        AND gr.receipt_date <= %s
                  )
                ORDER BY expected_date, po_id
                """,
                (STORE_ID, SKU, BUSINESS_DAY, BUSINESS_DAY, BUSINESS_DAY),
            )
            overdue = cur.fetchall()

    if len(overdue) != case["overdue_po_count"]:
        raise ValueError("PO count does not reconcile with the exception.")
    if sum(po["ordered_units"] for po in overdue) != case["overdue_units"]:
        raise ValueError("Overdue units do not reconcile with the exception.")

    evidence = {
        "business_day": str(case["business_day"]),
        "store_id": case["store_id"],
        "sku": case["sku"],
        "requested_units": case["requested_units"],
        "unmet_units": case["unmet_units"],
        "closing_units": case["closing_units"],
        "overdue_po_count": case["overdue_po_count"],
        "overdue_units": case["overdue_units"],
        "overdue_purchase_orders": [
            {
                "po_id": po["po_id"],
                "order_date": str(po["order_date"]),
                "expected_date": str(po["expected_date"]),
                "ordered_units": po["ordered_units"],
                "po_status": po["po_status"],
            }
            for po in overdue
        ],
    }

    print("✓ PostgreSQL evidence reconciled.")
    print("✓ Operating rule [EXP-01] loaded.")
    print("⏳ Asking the local Ollama model for a draft; this may take a few minutes.")

    model = ChatOllama(
        model="qwen3.5:4b",
        temperature=0,
        reasoning=False,
        num_ctx=2048,
        num_predict=220,
    ).with_structured_output(ProposedBrief, method="json_schema")

    brief = model.invoke(
        "You are assisting a pharmacy supply chain analyst in a SYNTHETIC "
        "simulation. Draft a short exception brief from the verified facts "
        "and operating rule below. Do not invent dates, numbers, supplier "
        "promises, receipts, or transfer availability. Do not place an order "
        "or claim an action was completed. Cite [EXP-01] in the reason. "
        "If an overdue supplier PO exists, choose ESCALATE_FOR_ETA and ask "
        "the buyer to confirm its ETA. A human must approve any action.\n\n"
        f"VERIFIED FACTS:\n{json.dumps(evidence, indent=2)}\n\n"
        f"OPERATING RULE [EXP-01]:\n{rule['text']}"
    )

    review_text = brief.reason + " " + brief.open_question
    known_ids = [
        rule["rule_id"],
        case["store_id"],
        case["sku"],
        *(po["po_id"] for po in overdue),
    ]
    for known_id in known_ids:
        review_text = review_text.replace(known_id, "")

    if re.search(r"\d", review_text):
        print(
            "\nREJECTED MODEL DRAFT — NOT AN APPROVED BRIEF:\n"
            + brief.model_dump_json(indent=2)
        )
        raise ValueError(
            "REVIEW: model used a number outside the verified IDs"
        )

    if overdue and brief.action != "ESCALATE_FOR_ETA":
        print(
            "\nREJECTED MODEL DRAFT — NOT AN APPROVED BRIEF:\n"
            + brief.model_dump_json(indent=2)
        )
        raise ValueError("REVIEW: action conflicts with the overdue-PO rule")

    result = {
        "status": "DRAFT_FOR_HUMAN_REVIEW",
        "verified_evidence": evidence,
        "cited_rule": rule["rule_id"],
        "suggested_action": brief.action,
        "reason": brief.reason,
        "question_for_analyst": brief.open_question,
        "purchase_order_placed": False,
    }
    print("\n✓ Grounded draft ready for human review:\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()