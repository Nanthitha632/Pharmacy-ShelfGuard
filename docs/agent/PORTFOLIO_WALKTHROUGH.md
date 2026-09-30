# Pharmacy ShelfGuard | AI case walkthrough

Synthetic pharmacy-network simulation. Airflow prepares daily demand, inventory, forecasts, purchase recommendations and exceptions in PostgreSQL. This local Streamlit control room invokes an eight-node LangGraph: read-only evidence, filtered vector rule retrieval, LLM draft, claim validation, donor check, reconciliation and provenance. A local Ollama model routes follow-up questions; displayed answers use verified facts and rule IDs. The agent does not place purchase orders or execute transfers. Human review remains required.

Demo: select 2026-05-07 ST036/MED026 (shortage without overdue PO), then ST040/MED001 (overdue supplier order). Show evidence, rule IDs, donor reserve, audit log and the four-scenario evaluation report.

Important limitation: the donor stock is a simulated candidate, not committed or approved; the numeric guard catches invented numbers but cannot prove all wording correct. The four scenarios are smoke checks, not broad model validation. The historical April holdout was 91.78% baseline fill versus 91.89% policy fill, and stockout store-SKU days were 3565 versus 3594. The original 96.1% and 18% improvement goals were not achieved.
