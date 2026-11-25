-- NewCo commodity-trading corpus: structured tables (Postgres). Generated; do not edit.
CREATE SCHEMA IF NOT EXISTS trading;

-- Every legal entity in universe.json: NewCo entities, counterparties, holdings, banks, service parties.
CREATE TABLE trading.counterparties (
    org_id VARCHAR(40) NOT NULL,
    name VARCHAR(120) NOT NULL,
    legal_form VARCHAR(40),
    kind VARCHAR(20) NOT NULL,
    country VARCHAR(4),
    lei VARCHAR(20),
    tax_id VARCHAR(40),
    jurisdiction VARCHAR(60),
    incorporation_date DATE,
    class_uri VARCHAR(200),
    PRIMARY KEY (org_id)
);

-- Bank accounts from universe.json with their identifiers.
CREATE TABLE trading.accounts (
    account_id VARCHAR(60) NOT NULL,
    owner_org_id VARCHAR(40) NOT NULL,
    bank_org_id VARCHAR(40),
    currency VARCHAR(3) NOT NULL,
    purpose VARCHAR(30),
    account_name VARCHAR(120),
    account_number VARCHAR(40),
    bic VARCHAR(11),
    iban VARCHAR(34),
    aba VARCHAR(9),
    bsb VARCHAR(7),
    class_uri VARCHAR(200),
    PRIMARY KEY (account_id),
    FOREIGN KEY (owner_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (bank_org_id) REFERENCES trading.counterparties (org_id)
);

-- Credit limits and agreement terms approved in each onboarding scenario.
CREATE TABLE trading.limits (
    scenario_id VARCHAR(60) NOT NULL,
    entity_org_id VARCHAR(40) NOT NULL,
    counterparty_org_id VARCHAR(40) NOT NULL,
    agreement VARCHAR(200),
    requested NUMERIC(20,2),
    unsecured NUMERIC(20,2),
    guaranteed NUMERIC(20,2),
    sblc NUMERIC(20,2),
    credit_limit NUMERIC(20,2),
    exposure NUMERIC(20,2),
    headroom NUMERIC(20,2),
    payment_days INT,
    governing_law VARCHAR(80),
    effective_date DATE,
    PRIMARY KEY (scenario_id),
    FOREIGN KEY (entity_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (counterparty_org_id) REFERENCES trading.counterparties (org_id)
);

-- Every deal in the trade phase, with its credit check outcome.
CREATE TABLE trading.deals (
    trade_id VARCHAR(30) NOT NULL,
    scenario_id VARCHAR(60) NOT NULL,
    entity_org_id VARCHAR(40) NOT NULL,
    counterparty_org_id VARCHAR(40) NOT NULL,
    kind VARCHAR(20),
    side VARCHAR(20),
    product VARCHAR(60),
    quantity NUMERIC(20,3),
    unit VARCHAR(10),
    currency VARCHAR(3),
    notional NUMERIC(20,2),
    status VARCHAR(20),
    credit_status VARCHAR(30),
    terms_json VARCHAR(2000),
    PRIMARY KEY (trade_id),
    FOREIGN KEY (entity_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (counterparty_org_id) REFERENCES trading.counterparties (org_id)
);

-- The last cash-bearing post-trade document of each trade (invoice, credit note, margin call, confirmation).
CREATE TABLE trading.invoices (
    document_number VARCHAR(60) NOT NULL,
    trade_id VARCHAR(30) NOT NULL,
    kind VARCHAR(30),
    issuer_org_id VARCHAR(40),
    payer_org_id VARCHAR(40),
    payee_org_id VARCHAR(40),
    currency VARCHAR(3),
    payable NUMERIC(20,2),
    due_date DATE,
    payer_account_id VARCHAR(60),
    payee_account_id VARCHAR(60),
    filename VARCHAR(120),
    PRIMARY KEY (document_number),
    FOREIGN KEY (trade_id) REFERENCES trading.deals (trade_id),
    FOREIGN KEY (issuer_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (payer_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (payee_org_id) REFERENCES trading.counterparties (org_id),
    FOREIGN KEY (payer_account_id) REFERENCES trading.accounts (account_id),
    FOREIGN KEY (payee_account_id) REFERENCES trading.accounts (account_id)
);

-- Cash movements from the settlement phase.
CREATE TABLE trading.payments (
    payment_id VARCHAR(70) NOT NULL,
    trade_id VARCHAR(30) NOT NULL,
    scenario_id VARCHAR(60) NOT NULL,
    amount NUMERIC(20,2),
    currency VARCHAR(3),
    value_date DATE,
    due_date DATE,
    days_late INT,
    interest NUMERIC(20,2),
    collateral NUMERIC(20,2),
    status VARCHAR(30),
    PRIMARY KEY (payment_id),
    FOREIGN KEY (trade_id) REFERENCES trading.deals (trade_id)
);

-- Ownership edges: group structure from the universe and the per-scenario edges found in onboarding.
CREATE TABLE trading.ownership_edges (
    edge_id VARCHAR(20) NOT NULL,
    scenario_id VARCHAR(60),
    parent_id VARCHAR(40) NOT NULL,
    child_id VARCHAR(40) NOT NULL,
    percent NUMERIC(7,3),
    relationship VARCHAR(40),
    class_uri VARCHAR(200),
    PRIMARY KEY (edge_id)
);
