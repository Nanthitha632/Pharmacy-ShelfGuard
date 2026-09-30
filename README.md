# 💊 Pharmacy ShelfGuard
### Forecast-Driven Replenishment and Supplier Risk

An end-to-end synthetic pharmacy supply chain simulation covering demand, forecasting, procurement, goods receipts, inventory, and exception management.

All data is simulated. No real pharmacy, patient, or Walgreens data is used. SAP S/4HANA MM concepts are modeled through simulated documents and SQL processes.

## Business Story

Customers request medicines while stores face shortages and supplier delays. ShelfGuard helps an analyst investigate service gaps, evaluate replenishment proposals, monitor overdue orders, and identify potential transfer donors.

## Workflow and Tools

| Process | Tools |
|---|---|
| Synthetic data generation and validation | Python |
| Procurement and inventory records | PostgreSQL and SQL |
| Demand forecasts and safety stock recommendations | Python |
| Daily workflow orchestration | Apache Airflow and Docker |
| Supply chain control tower | Power BI |
| AI exception investigation | LangGraph, LangChain, Ollama and Streamlit |

Power BI uses Import mode with manual Desktop refresh.

## AI Exception Control Room

The local agent reads verified PostgreSQL records, retrieves relevant operating rules through vector RAG, drafts a case brief, validates it, and checks potential donor stock.

- Supplier cases: identify overdue purchase orders and request an updated ETA.
- Shortage cases: support purchasing review and potential transfer investigation.
- Donor checks: protect a simulated three-day stock reserve.
- Interactive questions: use fast keyword routing and answers assembled from selected case evidence.
- Governance: require human review; the AI app does not execute purchases or transfers.

Four focused checks passed: no-overdue, overdue, missing-data, and an injected invented numeric claim. These are prototype checks, not production certification.

## Historical Simulation Results

April evaluation: April 1–30, 2026.

| Metric | Baseline | Escalation policy |
|---|---:|---:|
| Unit fill rate | 91.78% | 91.89% |
| Stockout store-SKU days | 3,565 | 3,594 |

Average daily inventory value decreased by 1.61%. Stockout days increased by 0.81%, despite the small fill-rate gain.

The original goals of 96.1% fill rate and 18% fewer stockout days were not achieved. Results are synthetic counterfactual outcomes, not observed pharmacy outcomes.

## Project Files

- `python/` — data generation, forecasts, simulations and AI application
- `sql/` — database definitions and reusable queries
- `automation/` — Airflow workflow files
- `data/` — synthetic inputs and simulation outputs
- `reports/` — metrics, validations and evaluation results
- `powerbi/` — dashboard artifacts
- `docs/` — operating rules and policy decisions

## Local AI Demo

Requires the populated local PostgreSQL database, Python dependencies, Ollama, and the models `qwen3.5:4b` and `nomic-embed-text`.

Run from the project folder:

```powershell
python -m streamlit run .\python\agent\app.py --server.address 127.0.0.1
```

Open http://127.0.0.1:8501 and investigate these cases for May 7, 2026:

- `ST036 / MED026` — shortage without an overdue purchase order
- `ST040 / MED001` — shortage with an overdue supplier order

## Limitations

Supplier escalation, costs and donor reserves are simulation assumptions. Potential transfers require physical stock, commitment, transport and approval checks. Numeric validation does not guarantee every statement is correct.