CREATE SCHEMA IF NOT EXISTS shelfguard;

CREATE TABLE IF NOT EXISTS shelfguard.stores (
    store_id text PRIMARY KEY,
    region text NOT NULL
);

CREATE TABLE IF NOT EXISTS shelfguard.suppliers (
    supplier_id text PRIMARY KEY,
    supplier_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS shelfguard.products (
    sku text PRIMARY KEY,
    product_name text NOT NULL,
    category text NOT NULL,
    supplier_id text NOT NULL REFERENCES shelfguard.suppliers(supplier_id),
    unit_cost_usd numeric(10, 2) NOT NULL CHECK (unit_cost_usd >= 0),
    case_pack integer NOT NULL CHECK (case_pack > 0),
    base_daily_demand integer NOT NULL CHECK (base_daily_demand >= 0)
);

CREATE TABLE IF NOT EXISTS shelfguard.customer_demand (
    demand_date date NOT NULL,
    store_id text NOT NULL REFERENCES shelfguard.stores(store_id),
    sku text NOT NULL REFERENCES shelfguard.products(sku),
    requested_units integer NOT NULL CHECK (requested_units >= 0),
    PRIMARY KEY (demand_date, store_id, sku)
);

CREATE TABLE IF NOT EXISTS shelfguard.purchase_orders (
    po_id text PRIMARY KEY,
    order_date date NOT NULL,
    expected_date date NOT NULL,
    actual_receipt_date date,
    store_id text NOT NULL REFERENCES shelfguard.stores(store_id),
    sku text NOT NULL REFERENCES shelfguard.products(sku),
    supplier_id text NOT NULL REFERENCES shelfguard.suppliers(supplier_id),
    ordered_units integer NOT NULL CHECK (ordered_units > 0),
    CHECK (expected_date >= order_date)
);

CREATE TABLE IF NOT EXISTS shelfguard.goods_receipts (
    receipt_id text PRIMARY KEY,
    po_id text NOT NULL REFERENCES shelfguard.purchase_orders(po_id),
    receipt_date date NOT NULL,
    store_id text NOT NULL REFERENCES shelfguard.stores(store_id),
    sku text NOT NULL REFERENCES shelfguard.products(sku),
    accepted_units integer NOT NULL CHECK (accepted_units > 0)
);

CREATE TABLE IF NOT EXISTS shelfguard.baseline_daily (
    inventory_date date NOT NULL,
    store_id text NOT NULL REFERENCES shelfguard.stores(store_id),
    sku text NOT NULL REFERENCES shelfguard.products(sku),
    opening_units integer NOT NULL CHECK (opening_units >= 0),
    received_units integer NOT NULL CHECK (received_units >= 0),
    requested_units integer NOT NULL CHECK (requested_units >= 0),
    fulfilled_units integer NOT NULL CHECK (fulfilled_units >= 0),
    unmet_units integer NOT NULL CHECK (unmet_units >= 0),
    ordered_units integer NOT NULL CHECK (ordered_units >= 0),
    closing_units integer NOT NULL CHECK (closing_units >= 0),
    PRIMARY KEY (inventory_date, store_id, sku),
    CHECK (fulfilled_units + unmet_units = requested_units),
    CHECK (
        opening_units + received_units - fulfilled_units
        = closing_units
    )
);