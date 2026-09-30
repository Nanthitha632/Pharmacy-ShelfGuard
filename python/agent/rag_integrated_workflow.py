"""Integrated ShelfGuard agent with filtered semantic rule retrieval."""

from __future__ import annotations

from functools import lru_cache
from getpass import getpass
from pathlib import Path
from typing import TypedDict
import json
import re

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_ollama import OllamaEmbeddings
from langgraph.graph import START, END, StateGraph

from integrated_workflow import check_donors, reconcile_case
from workflow import fetch_evidence, draft_brief, validate_brief


ROOT = Path(__file__).resolve().parents[2]
RULEBOOK = ROOT / "docs" / "agent" / "shelfguard_operating_rules.md"


class RagState(TypedDict, total=False):
    business_day: str
    store_id: str
    sku: str
    password: str
    evidence: dict
    rule_ids: list[str]
    rule_text: str
    semantic_matches: list[str]
    mandatory_rules: list[str]
    draft: dict
    result: dict
    brief: dict
    donors: dict
    final: dict


@lru_cache(maxsize=1)
def rule_index():
    """Embed the synthetic rules once per Python process."""
    text = RULEBOOK.read_text(encoding="utf-8")
    pattern = (
        r"(?ms)^#{1,4}[ \t]*[^\n]*?"
        r"\[([A-Z]{3}-\d{2})\][^\n]*\n.*?"
        r"(?=^#{1,4}[ \t]|\Z)"
    )

    documents = [
        Document(
            page_content=match.group(0).strip(),
            metadata={"rule_id": match.group(1)},
        )
        for match in re.finditer(pattern, text)
    ]
    by_id = {doc.metadata["rule_id"]: doc for doc in documents}

    required_ids = {"EXP-01", "BUY-01", "TRF-01", "GOV-01"}
    if not required_ids.issubset(by_id):
        raise ValueError(
            f"Rulebook missing: {sorted(required_ids - set(by_id))}"
        )

    embeddings = OllamaEmbeddings(model="nomic-embed-text")
    store = InMemoryVectorStore(embeddings)
    store.add_documents(documents)

    print(f"Indexed {len(documents)} synthetic operating rules.")
    return store, by_id


def retrieve_rules_semantically(state: RagState) -> dict:
    print("2/7  Searching and filtering operating rules...")

    evidence = state["evidence"]
    overdue = evidence["overdue_po_count"] > 0

    if overdue:
        query = (
            "Customer demand is unmet and a supplier purchase order "
            "is overdue. How should a buyer confirm ETA and escalate?"
        )
        mandatory = ["EXP-01"]
    else:
        query = (
            "Customer demand is unmet with no overdue supplier order. "
            "Review replenishment proposals and potential store transfer "
            "while protecting donor stock."
        )
        mandatory = ["BUY-01", "TRF-01"]

    store, by_id = rule_index()
    matches = store.similarity_search(query, k=2)
    ranked_ids = [doc.metadata["rule_id"] for doc in matches]

    # Mandatory rules are always included. Semantic retrieval may add
    # general governance guidance, but cannot inject a rule for the
    # wrong business situation (such as EXP-01 with no overdue PO).
    selected_ids = list(mandatory)
    for rule_id in ranked_ids:
        if rule_id == "GOV-01" and rule_id not in selected_ids:
            selected_ids.append(rule_id)

    return {
        "rule_ids": selected_ids,
        "rule_text": "\n\n".join(
            by_id[rule_id].page_content
            for rule_id in selected_ids
        ),
        "semantic_matches": ranked_ids,
        "mandatory_rules": mandatory,
    }


def prepare_brief(state: RagState) -> dict:
    return {"brief": state["result"]}


def add_retrieval_provenance(state: RagState) -> dict:
    final = dict(state["final"])
    final["rag"] = {
        "method": "local Ollama embeddings + in-memory vector search",
        "semantic_matches_in_rank_order": state["semantic_matches"],
        "mandatory_case_rules": state["mandatory_rules"],
        "rules_supplied_to_llm": state["rule_ids"],
        "rulebook": str(RULEBOOK.relative_to(ROOT)),
    }
    final["human_approval_required"] = True
    return {"final": final}


builder = StateGraph(RagState)
builder.add_node("fetch_evidence", fetch_evidence)
builder.add_node("semantic_rule_search", retrieve_rules_semantically)
builder.add_node("draft_brief", draft_brief)
builder.add_node("validate_brief", validate_brief)
builder.add_node("prepare_brief", prepare_brief)
builder.add_node("check_donors", check_donors)
builder.add_node("reconcile_case", reconcile_case)
builder.add_node("add_retrieval_provenance", add_retrieval_provenance)

builder.add_edge(START, "fetch_evidence")
builder.add_edge("fetch_evidence", "semantic_rule_search")
builder.add_edge("semantic_rule_search", "draft_brief")
builder.add_edge("draft_brief", "validate_brief")
builder.add_edge("validate_brief", "prepare_brief")
builder.add_edge("prepare_brief", "check_donors")
builder.add_edge("check_donors", "reconcile_case")
builder.add_edge("reconcile_case", "add_retrieval_provenance")
builder.add_edge("add_retrieval_provenance", END)

rag_agent = builder.compile()


if __name__ == "__main__":
    password = getpass("PostgreSQL password: ")
    output = rag_agent.invoke(
        {
            "business_day": "2026-05-07",
            "store_id": "ST036",
            "sku": "MED026",
            "password": password,
        }
    )
    print("\nRAG CASE HANDOFF")
    print(json.dumps(output["final"], indent=2))