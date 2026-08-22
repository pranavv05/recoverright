-- database/schema.sql

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id VARCHAR PRIMARY KEY,
    client_name VARCHAR,
    amount_paise BIGINT CHECK (amount_paise >= 0),
    outstanding_balance_paise BIGINT CHECK (outstanding_balance_paise >= 0),
    days_overdue INT,
    account_value_tier VARCHAR,
    scenario_type VARCHAR,
    true_cause VARCHAR,
    client_notes VARCHAR,
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    invoice_id VARCHAR REFERENCES invoices(invoice_id) ON DELETE CASCADE,
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
    invoice_id VARCHAR PRIMARY KEY REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    payment_link_id VARCHAR,
    payment_link_url VARCHAR,
    status VARCHAR,
    amount_requested_paise BIGINT,
    amount_recovered_paise BIGINT DEFAULT 0 CHECK (amount_recovered_paise >= 0),
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- Money is only ever counted as recovered by execution.actions.reconcile_payment,
-- after Razorpay reports the link as paid. Enforced here too so no future code
-- path can mark a row verified without an amount, or credit an amount without
-- marking it verified.
ALTER TABLE recoveries DROP CONSTRAINT IF EXISTS recovered_only_when_verified;
ALTER TABLE recoveries ADD CONSTRAINT recovered_only_when_verified CHECK (
    (status = 'payment_verified' AND amount_recovered_paise > 0 AND verified_at IS NOT NULL)
    OR (status <> 'payment_verified' AND amount_recovered_paise = 0)
);

CREATE INDEX IF NOT EXISTS audit_log_invoice_created_idx
    ON audit_log (invoice_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_log_event_type_idx ON audit_log (event_type);
