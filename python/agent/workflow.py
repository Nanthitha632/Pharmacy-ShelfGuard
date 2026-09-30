"""ShelfGuard read-only agent: evidence → rules → draft → validation."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict
import json
import re

import psycopg
from langchain_ollama import ChatOllama
from langgraph.graph import START, END, StateGraph
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = ROOT / "docs" / "agent" / "shelfguard_operating_rules.md"


class Draft(BaseModel):
    action: Literal["ESCALATE_FOR_ETA", "HOLD_FOR_REVIEW"]
    reason: str = Field(description="A short explanation of this case.")
    question: str = Field(description="One question for a human analyst.")


class AgentState(TypedDict, total=False):
    business_day: str
    store_id: str
    sku: str
    password: str
    evidence: dict
    rule_ids: list[str]
    rule_text: str
    draft: dict
    result: dict


def fetch_evidence(state: AgentState) -> dict:
    print("1/4  Reading verified database facts...")

    with psycopg.connect(
        host="localhost",
        port=5433,
        dbname="pharmacy_shelfguard_fresh",
        user="postgres",
        password=state["password"],
        options="-c default_transaction_read_only=on",
        row_factory=psycopg.rows.dict_row,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT business_day, store_id, sku, requested_units,
                       unmet_units, closing_units, overdue_po_count,
                       overdue_units
                FROM shelfguard.v_daily_exceptions_v3
                WHERE business_day = %s
                  AND store_id = %s
                  AND sku = %s
                """,
                (
                    state["business_day"],
                    state["store_id"],
                    state["sku"],
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("No exception exists for this day/store/SKU.")

            cur.execute(
                """
                SELECT po.po_id, po.expected_date, po.ordered_units
                FROM shelfguard.purchase_orders_v3 AS po
                WHERE po.store_id = %s
                  AND po.sku = %s
                  AND po.order_date <= %s
                  AND po.expected_date < %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM shelfguard.goods_receipts_v3 AS gr
                      WHERE gr.po_id = po.po_id
                        AND gr.receipt_date <= %s
                  )
                ORDER BY po.expected_date, po.po_id
                """,
                (
                    state["store_id"],
                    state["sku"],
                    state["business_day"],
                    state["business_day"],
                    state["business_day"],
                ),
            )
            orders = cur.fetchall()

    if len(orders) != row["overdue_po_count"]:
        raise ValueError("Overdue PO count did not reconcile.")
    if sum(po["ordered_units"] for po in orders) != row["overdue_units"]:
        raise ValueError("Overdue PO units did not reconcile.")

    evidence = {
        "business_day": str(row["business_day"]),
        "store_id": row["store_id"],
        "sku": row["sku"],
        "requested_units": row["requested_units"],
        "unmet_units": row["unmet_units"],
        "closing_units": row["closing_units"],
        "overdue_po_count": row["overdue_po_count"],
        "overdue_units": row["overdue_units"],
        "overdue_orders": [
            {
                "po_id": po["po_id"],
                "expected_date": str(po["expected_date"]),
                "ordered_units": po["ordered_units"],
            }
            for po in orders
        ],
    }
    return {"evidence": evidence}


def retrieve_rules(state: AgentState) -> dict:
    print("2/4  Retrieving relevant operating rules...")

    if state["evidence"]["overdue_po_count"] > 0:
        rule_ids = ["EXP-01"]
    else:
        rule_ids = ["BUY-01", "TRF-01"]

    rulebook = RULES_FILE.read_text(encoding="utf-8")
    sections = []

    for rule_id in rule_ids:
        match = re.search(
            rf"(?ms)^#{{1,4}}[ \t]*[^\n]*?\[{rule_id}\][^\n]*\n.*?"
            r"(?=^#{1,4}[ \t]|\Z)",
            rulebook,
        )
        if match is None:
            raise ValueError(f"Rule [{rule_id}] is missing from the rulebook.")
        sections.append(match.group(0).strip())

    return {
        "rule_ids": rule_ids,
        "rule_text": "\n\n".join(sections),
    }


def draft_brief(state: AgentState) -> dict:
    print("3/4  Drafting with the local LLM...")

    evidence = state["evidence"]
    overdue = evidence["overdue_po_count"] > 0

    if overdue:
        instruction = (
            "Choose ESCALATE_FOR_ETA. Ask the buyer to confirm the "
            "supplier ETA. Cite [EXP-01]. An overdue PO and unmet demand "
            "coexist; do not claim one caused the other."
        )
    else:
        instruction = (
            "Choose HOLD_FOR_REVIEW. Ask whether a purchase proposal "
            "exists and whether a transfer is feasible. Cite [BUY-01] "
            "and [TRF-01] when relevant. Donor stock has NOT been checked: "
            "do not claim that it exists or does not exist."
        )

    model = ChatOllama(
        model="qwen3.5:4b",
        temperature=0,
        reasoning=False,
        num_ctx=2048,
        num_predict=180,
    ).with_structured_output(Draft, method="json_schema")

    draft = model.invoke(
        "You assist a human pharmacy supply chain analyst in a SYNTHETIC "
        "simulation. Propose one action and one useful question for the "
        "analyst. Use only verified facts and retrieved rules. Never "
        "invent numbers, dates, supplier promises, donor inventory, "
        "receipts, or actions already completed. "
        f"{instruction}\n\n"
        f"VERIFIED EVIDENCE:\n{json.dumps(evidence, indent=2)}\n\n"
        f"RETRIEVED RULES:\n{state['rule_text']}"
    )
    return {"draft": draft.model_dump()}


def validate_brief(state: AgentState) -> dict:
    print("4/4  Validating facts, action, and citations...")

    evidence = state["evidence"]
    draft = state["draft"]
    rule_ids = state["rule_ids"]
    overdue = evidence["overdue_po_count"] > 0
    expected_action = (
        "ESCALATE_FOR_ETA" if overdue else "HOLD_FOR_REVIEW"
    )

    issues = []
    if draft["action"] != expected_action:
        issues.append("The action conflicts with verified PO status.")

    prose = draft["reason"] + " " + draft["question"]
    citations = re.findall(r"\[([A-Z]{3}-\d{2})\]", prose)
    if any(citation not in rule_ids for citation in citations):
        issues.append("The draft cites a rule that was not retrieved.")

    # Citations are checked above. Remove them before checking numerals.
    remaining = re.sub(r"\[[A-Z]{3}-\d{2}\]", "", prose)

    verified_strings = {
        evidence["business_day"],
        evidence["store_id"],
        evidence["sku"],
        str(evidence["requested_units"]),
        str(evidence["unmet_units"]),
        str(evidence["closing_units"]),
        str(evidence["overdue_po_count"]),
        str(evidence["overdue_units"]),
    }
    for po in evidence["overdue_orders"]:
        verified_strings.update(
            {
                po["po_id"],
                po["expected_date"],
                str(po["ordered_units"]),
            }
        )

    for value in sorted(verified_strings, key=len, reverse=True):
        remaining = remaining.replace(value, "")
    if re.search(r"\d", remaining):
        issues.append("The draft contains a number absent from the evidence.")

    lower = prose.lower()
    unsupported_claims = (
        "no verified donor stock exists",
        "no donor stock exists",
        "donor stock is available",
        "transfer is available",
        "transfer is not feasible",
        "supplier confirmed",
        "supplier promised",
        "purchase order was placed",
        "transfer was executed",
        "goods were received",
        "creating a shortage",
        "causing the shortage",
        "caused the shortage",
    )
    if any(phrase in lower for phrase in unsupported_claims):
        issues.append("The draft asserts an outcome not verified by the data.")

    # The displayed explanation comes from SQL facts, never LLM prose.
    if overdue:
        reason = (
            f"On {evidence['business_day']}, "
            f"{evidence['store_id']} / {evidence['sku']} had "
            f"{evidence['unmet_units']} unmet units and "
            f"{evidence['overdue_units']} units on overdue POs. "
            "The buyer should confirm the supplier ETA [EXP-01]."
        )
        safe_question = "Can the buyer confirm the supplier's updated ETA?"
    else:
        reason = (
            f"On {evidence['business_day']}, "
            f"{evidence['store_id']} / {evidence['sku']} had "
            f"{evidence['unmet_units']} unmet units and no overdue POs. "
            "Review purchasing options [BUY-01] and check whether "
            "a transfer is feasible [TRF-01]."
        )
        safe_question = (
            "Is there a purchase proposal, and does another store have "
            "transferable stock after its own reserve is protected?"
        )

    if issues:
        result = {
            "status": "REJECTED_FOR_REVIEW",
            "case": evidence,
            "rule_id": rule_ids[0],
            "rule_ids": rule_ids,
            "suggested_action": "HOLD_FOR_REVIEW",
            "reason": reason,
            "question_for_analyst": safe_question,
            "review_issues": issues,
            "rejected_model_draft": draft,
            "purchase_order_placed": False,
        }
    else:
        result = {
            "status": "DRAFT_FOR_HUMAN_REVIEW",
            "case": evidence,
            "rule_id": rule_ids[0],
            "rule_ids": rule_ids,
            "suggested_action": expected_action,
            "reason": reason,
            "question_for_analyst": draft["question"],
            "review_issues": [],
            "purchase_order_placed": False,
        }

    return {"result": result}


builder = StateGraph(AgentState)
builder.add_node("fetch_evidence", fetch_evidence)
builder.add_node("retrieve_rules", retrieve_rules)
builder.add_node("draft_brief", draft_brief)
builder.add_node("validate_brief", validate_brief)

builder.add_edge(START, "fetch_evidence")
builder.add_edge("fetch_evidence", "retrieve_rules")
builder.add_edge("retrieve_rules", "draft_brief")
builder.add_edge("draft_brief", "validate_brief")
builder.add_edge("validate_brief", END)

agent = builder.compile()


if __name__ == "__main__":
    from getpass import getpass

    password = getpass("PostgreSQL password: ")
    final_state = agent.invoke(
        {
            "business_day": "2026-05-07",
            "store_id": "ST036",
            "sku": "MED026",
            "password": password,
        }
    )
    print(json.dumps(final_state["result"], indent=2))