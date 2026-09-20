-- Rosebud persistent store schema (Postgres).
-- Applied automatically on first connect when ROSEBUD_STORE=postgres.
-- Money is stored as integer cents and timestamps as timestamptz.

CREATE TABLE IF NOT EXISTS quotes (
    quote_id   TEXT PRIMARY KEY,
    payload    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS quotes_expires_at_idx ON quotes (expires_at);

CREATE TABLE IF NOT EXISTS payments (
    payment_ref           TEXT PRIMARY KEY,
    amount_cents          INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'captured'
                          CHECK (status IN ('captured', 'refunded')),
    order_id              TEXT UNIQUE,
    florist_charged_cents INTEGER,
    fee_kept_cents        INTEGER,
    refund_id             TEXT,
    captured_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    refunded_at           TIMESTAMPTZ
);
