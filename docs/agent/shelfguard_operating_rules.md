# ShelfGuard Operating Rules — Synthetic Simulation
Version: 1.0
Purpose: Training rules for a fictional pharmacy network.
These are project assumptions, not a real company's policies or SAP configuration.

## [EXP-01] Overdue supplier purchase orders
A purchase order is overdue on a business day when its expected date
has passed and ordered units remain unreceived as of that day.
An overdue PO with unmet customer demand requires buyer review.
The buyer should request a confirmed supplier ETA and assess whether
expediting is possible. Do not assume that escalation guarantees a
next-day receipt. Show the PO ID, expected date, open units, and the
date through which receipts were checked.

## [TRF-01] Store-to-store transfer review
Consider a transfer only after checking another store's stock for the
same SKU on the same business day. Protect the donor store's forecast
demand and safety stock before calculating transferable units.
If any required donor data is missing, mark the option "needs review."
A suggested transfer is not an approved or executed transfer.

## [BUY-01] New purchase proposal review
Compare a proposed order with existing open POs to avoid counting
incoming stock twice. Show forecast demand, lead time, safety stock,
case-pack rounding, estimated cost, and approval-queue status.
Only an authorized reviewer may approve or issue a simulated PO.

## [GOV-01] Evidence and decision control
Every decision brief must identify the business date, store, SKU,
supporting database records, and the rule IDs used. Separate observed
facts from assumptions and proposed actions. If the database and an
exception summary disagree, pause the recommendation for data review.
The AI agent may investigate and draft a brief; it may not issue POs,
post goods receipts, or execute transfers.