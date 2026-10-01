-- 0001: append-only event log plus the projections derived from it (plan M1.5).
-- Money and prices are TEXT holding exact decimals. Timestamps are ISO-8601 UTC text.

CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc         TEXT    NOT NULL,
    ist_date       TEXT    NOT NULL,
    type           TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    decision_id    TEXT,
    cycle_id       TEXT,
    book_id        TEXT,
    symbol         TEXT,
    source         TEXT    NOT NULL,
    payload        TEXT    NOT NULL
);
CREATE INDEX ix_events_type ON events (type, seq);
CREATE INDEX ix_events_decision ON events (decision_id, seq) WHERE decision_id IS NOT NULL;
CREATE INDEX ix_events_symbol ON events (symbol, seq) WHERE symbol IS NOT NULL;
CREATE INDEX ix_events_book ON events (book_id, seq) WHERE book_id IS NOT NULL;
CREATE INDEX ix_events_ist_date ON events (ist_date, seq);

CREATE TABLE orders (
    client_order_id TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL,
    decision_id     TEXT NOT NULL,
    intent_id       TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    side            TEXT NOT NULL,
    product         TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    reason          TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    status          TEXT NOT NULL,
    filled_qty      INTEGER NOT NULL,
    avg_fill_price  TEXT,
    broker_order_id TEXT,
    last_message    TEXT,
    created_seq     INTEGER NOT NULL,
    updated_seq     INTEGER NOT NULL,
    updated_ts      TEXT NOT NULL
);
CREATE INDEX ix_orders_book_status ON orders (book_id, status);

CREATE TABLE fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    book_id         TEXT NOT NULL,
    decision_id     TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price           TEXT NOT NULL,
    charges         TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    seq             INTEGER NOT NULL
);
CREATE INDEX ix_fills_order ON fills (client_order_id);

CREATE TABLE positions (
    book_id        TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    product        TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    avg_price      TEXT,
    realized_pnl   TEXT NOT NULL,
    updated_ts     TEXT NOT NULL,
    updated_seq    INTEGER NOT NULL,
    PRIMARY KEY (book_id, instrument_key, product)
);

CREATE TABLE trades (
    trade_id         TEXT PRIMARY KEY,
    book_id          TEXT NOT NULL,
    decision_id      TEXT NOT NULL,
    exit_decision_id TEXT,
    instrument_key   TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    entry_price      TEXT NOT NULL,
    exit_price       TEXT NOT NULL,
    entry_ts         TEXT NOT NULL,
    exit_ts          TEXT NOT NULL,
    gross_pnl        TEXT NOT NULL,
    charges          TEXT NOT NULL,
    net_pnl          TEXT NOT NULL,
    exit_reason      TEXT NOT NULL,
    seq              INTEGER NOT NULL
);
CREATE INDEX ix_trades_book ON trades (book_id, exit_ts);

CREATE TABLE daily_risk_state (
    book_id     TEXT NOT NULL,
    ist_date    TEXT NOT NULL,
    state       TEXT NOT NULL,
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (book_id, ist_date)
);

CREATE TABLE kill_switches (
    book_id     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    name        TEXT NOT NULL,
    state       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    since_ts    TEXT NOT NULL,
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (book_id, scope, name)
);

CREATE TABLE decisions (
    book_id        TEXT NOT NULL,
    intent_id      TEXT NOT NULL,
    decision_id    TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    side           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    qty_approved   INTEGER NOT NULL,
    limits_hash    TEXT NOT NULL,
    payload        TEXT NOT NULL,
    ts_utc         TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    PRIMARY KEY (book_id, intent_id)
);
CREATE INDEX ix_decisions_decision ON decisions (decision_id);

CREATE TABLE llm_calls (
    seq            INTEGER PRIMARY KEY,
    ts_utc         TEXT NOT NULL,
    ist_date       TEXT NOT NULL,
    decision_id    TEXT,
    book_id        TEXT,
    role           TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    latency_ms     REAL NOT NULL,
    cost_usd       TEXT,
    cost_inr       TEXT,
    cache_hit      INTEGER NOT NULL
);
CREATE INDEX ix_llm_calls_day ON llm_calls (ist_date, role);

CREATE TABLE decision_model_calls (
    seq            INTEGER PRIMARY KEY,
    ts_utc         TEXT NOT NULL,
    ist_date       TEXT NOT NULL,
    decision_id    TEXT,
    book_id        TEXT,
    task           TEXT NOT NULL,
    model          TEXT NOT NULL,
    checkpoint     TEXT NOT NULL,
    latency_ms     REAL NOT NULL,
    escalated      INTEGER NOT NULL,
    shadow         INTEGER NOT NULL,
    calibrated     INTEGER NOT NULL,
    outcome        TEXT NOT NULL,
    answers        TEXT NOT NULL
);

CREATE TABLE typed_events (
    event_id          TEXT PRIMARY KEY,
    instrument_key    TEXT NOT NULL,
    published_at      TEXT NOT NULL,
    relevant          INTEGER NOT NULL,
    announcement_type TEXT,
    direction         TEXT,
    materiality       TEXT,
    payload           TEXT NOT NULL,
    seq               INTEGER NOT NULL
);
CREATE INDEX ix_typed_events_instrument ON typed_events (instrument_key, published_at);
