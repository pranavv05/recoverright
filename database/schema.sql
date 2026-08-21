-- database/schema.sql

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id VARCHAR PRIMARY KEY,
    client_name VARCHAR,
    amount_paise BIGINT,
    outstanding_balance_paise BIGINT,
    days_overdue INT,
    account_value_tier VARCHAR,
    scenario_type VARCHAR,
    true_cause VARCHAR,
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    invoice_id VARCHAR REFERENCES invoices(invoice_id),
    event_type VARCHAR,
    decision VARCHAR,
    reasons JSONB,
    allowed_actions JSONB,
    blocked_actions JSONB,
    diagnosis JSONB,
    execution_result JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS recoveries (
    invoice_id VARCHAR PRIMARY KEY REFERENCES invoices(invoice_id),
    payment_link_id VARCHAR,
    payment_link_url VARCHAR,
    status VARCHAR,
    amount_requested_paise BIGINT,
    amount_recovered_paise BIGINT DEFAULT 0,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);