-- RightAngle-style ETRM deal capture (separate datasource). Generated; do not edit.
CREATE SCHEMA IF NOT EXISTS rightangle;

-- Internal and external business associates (legal entities) known to the ETRM.
CREATE TABLE rightangle.business_associates (
    ba_code VARCHAR(20) NOT NULL,
    name VARCHAR(120) NOT NULL,
    org_id VARCHAR(40),
    ba_type VARCHAR(10) NOT NULL,
    PRIMARY KEY (ba_code)
);

-- ETRM product codes.
CREATE TABLE rightangle.products (
    product_code VARCHAR(20) NOT NULL,
    name VARCHAR(60) NOT NULL,
    PRIMARY KEY (product_code)
);

-- One row per deal version: parties, side, status and the reference to the email confirmation.
CREATE TABLE rightangle.deal_header (
    deal_number VARCHAR(20) NOT NULL,
    version INT NOT NULL,
    deal_date DATE NOT NULL,
    trader VARCHAR(20),
    internal_ba VARCHAR(20) NOT NULL,
    external_ba VARCHAR(20) NOT NULL,
    buy_sell VARCHAR(1) NOT NULL,
    deal_status VARCHAR(20) NOT NULL,
    external_ref VARCHAR(30),
    change_reason VARCHAR(200),
    PRIMARY KEY (deal_number, version),
    FOREIGN KEY (internal_ba) REFERENCES rightangle.business_associates (ba_code),
    FOREIGN KEY (external_ba) REFERENCES rightangle.business_associates (ba_code)
);

-- Commodity, delivery, quantity and price of each deal version.
CREATE TABLE rightangle.deal_detail (
    deal_number VARCHAR(20) NOT NULL,
    version INT NOT NULL,
    product_code VARCHAR(20) NOT NULL,
    location VARCHAR(60),
    delivery_term VARCHAR(10),
    period VARCHAR(10),
    quantity NUMERIC(20,3),
    uom VARCHAR(5),
    price_type VARCHAR(10),
    fixed_price NUMERIC(20,3),
    price_index VARCHAR(60),
    premium NUMERIC(20,3),
    currency VARCHAR(3),
    PRIMARY KEY (deal_number, version),
    FOREIGN KEY (product_code) REFERENCES rightangle.products (product_code),
    FOREIGN KEY (deal_number, version) REFERENCES rightangle.deal_header (deal_number, version)
);
