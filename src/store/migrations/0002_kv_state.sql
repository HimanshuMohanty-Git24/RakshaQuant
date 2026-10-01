-- 0002: key/value state for components that keep their own durable state outside the event log,
-- e.g. the simulated broker's exchange-side book (reconciled against the OMS, like a real broker).
-- Not a projection: rebuild_projections() never touches it.

CREATE TABLE kv_state (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);
