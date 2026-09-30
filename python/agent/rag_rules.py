"""Retrieve cited rules from ShelfGuard's synthetic operating guide."""

from pathlib import Path
import re

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_ollama import OllamaEmbeddings


RULEBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "agent"
    / "shelfguard_operating_rules.md"
)


def load_rules():
    """Keep each numbered rule as a separate, citable document."""
    text = RULEBOOK.read_text(encoding="utf-8")
    headings = list(
        re.finditer(
            r"^## \[([A-Z]+-\d+)\] (.+)$",
            text,
            flags=re.MULTILINE,
        )
    )
    if not headings:
        raise ValueError("No numbered rules found in the rulebook")

    documents = []
    for index, heading in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(text)
        )
        passage = text[heading.end():end].strip()
        documents.append(
            Document(
                page_content=f"{heading.group(2)}\n{passage}",
                metadata={
                    "rule_id": heading.group(1),
                    "source": RULEBOOK.name,
                    "synthetic": True,
                },
            )
        )
    return documents


def retrieve_rules(question, k=2):
    """Search locally using embeddings; return text and source IDs."""
    embeddings = OllamaEmbeddings(
        model="nomic-embed-text",
        base_url="http://localhost:11434",
    )
    index = InMemoryVectorStore(embedding=embeddings)
    rules = load_rules()
    index.add_documents(rules)
    return index.similarity_search(question, k=k)


if __name__ == "__main__":
    question = (
        "A supplier purchase order was expected yesterday, but no "
        "goods receipt has arrived and customers have unmet demand. "
        "What should the buyer check?"
    )
    results = retrieve_rules(question)

    print(f"Indexed {len(load_rules())} synthetic rules")
    print(f"\nQUESTION: {question}")
    for rank, rule in enumerate(results, start=1):
        print(
            f"\nMATCH {rank}: [{rule.metadata['rule_id']}] "
            f"from {rule.metadata['source']}"
        )
        print(rule.page_content)