-- One row per store and day: customer service and sellable stock.
CREATE OR REPLACE VIEW shelfguard.v_store_daily AS
SELECT
    d.inventory_date,
    d.store_id,
    s.region,
    SUM(d.requested_units)::bigint AS requested_units,
    SUM(d.fulfilled_units)::bigint AS fulfilled_units,
    SUM(d.unmet_units)::bigint AS unmet_units,
    ROUND(
        100.0 * SUM(d.fulfilled_units)
        / NULLIF(SUM(d.requested_units), 0), 2
    ) AS fill_rate_percent,
    COUNT(*) FILTER (WHERE d.unmet_units > 0)
        AS stockout_sku_count,
    ROUND(
        SUM(d.closing_units * p.unit_cost_usd), 2
    ) AS closing_inventory_value_usd,
    ROUND(
        SUM(d.closing_units)::numeric
        / NULLIF(SUM(p.base_daily_demand), 0), 2
    ) AS estimated_days_of_supply
FROM shelfguard.baseline_daily d
JOIN shelfguard.stores s
  ON s.store_id = d.store_id
JOIN shelfguard.products p
  ON p.sku = d.sku
GROUP BY d.inventory_date, d.store_id, s.region;

-- PO status at the end of the 60-day simulation.
-- A missing receipt stays open; future actual dates are not used.
CREATE OR REPLACE VIEW shelfguard.v_purchase_order_status AS
SELECT
    po.po_id,
    po.store_id,
    po.sku,
    po.supplier_id,
    su.supplier_name,
    po.order_date,
    po.expected_date,
    po.ordered_units,
    gr.receipt_date,
    gr.accepted_units,
    CASE
        WHEN gr.receipt_date IS NOT NULL
             AND gr.receipt_date <= po.expected_date
            THEN 'RECEIVED_ON_TIME'
        WHEN gr.receipt_date IS NOT NULL
            THEN 'RECEIVED_LATE'
        WHEN po.expected_date < DATE '2026-03-01'
            THEN 'OVERDUE_OPEN'
        ELSE 'OPEN_NOT_DUE'
    END AS po_status,
    CASE
        WHEN gr.receipt_date IS NOT NULL
            THEN GREATEST(
                gr.receipt_date - po.expected_date, 0
            )
        WHEN po.expected_date < DATE '2026-03-01'
            THEN DATE '2026-03-01' - po.expected_date
        ELSE 0
    END AS days_late
FROM shelfguard.purchase_orders po
JOIN shelfguard.suppliers su
  ON su.supplier_id = po.supplier_id
LEFT JOIN shelfguard.goods_receipts gr
  ON gr.po_id = po.po_id;

-- Quick verification after the views are created.
SELECT
    (SELECT COUNT(*) FROM shelfguard.v_store_daily)
        AS store_day_rows,
    (SELECT COUNT(*) FROM shelfguard.v_purchase_order_status)
        AS purchase_order_rows,
    (SELECT COUNT(*) FROM shelfguard.v_purchase_order_status
     WHERE po_status = 'OVERDUE_OPEN')
        AS overdue_open_orders;