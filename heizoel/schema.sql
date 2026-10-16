-- Schema Heizöl-Kaufberater. Wird von db.init_db() idempotent angewendet.
-- Preise immer in Cent je Liter, brutto, bezogen auf 3000 L Abnahme, sofern nicht anders benannt.
-- Datumsangaben als ISO-Text (YYYY-MM-DD), Zeitstempel als ISO-Text in UTC.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id                  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    plz                      TEXT NOT NULL,
    region                   TEXT NOT NULL,            -- erste zwei Stellen der PLZ
    tank_liter               INTEGER NOT NULL,
    fill_percent             REAL NOT NULL,            -- 0..100
    fill_date                TEXT NOT NULL,
    annual_consumption_liter INTEGER NOT NULL,
    min_order_liter          INTEGER NOT NULL DEFAULT 1000,
    lead_days                INTEGER NOT NULL DEFAULT 10,  -- Lieferzeit in Tagen
    notify_channel           TEXT NOT NULL DEFAULT 'none' CHECK (notify_channel IN ('mail', 'none')),
    threshold_percentile     INTEGER NOT NULL DEFAULT 20,
    offer_liter              INTEGER,                  -- Wunschmenge für Angebote, NULL = automatisch aus Bedarf
    learned_annual_liter     REAL,                     -- aus Füllständen gelernter Jahresverbrauch
    learned_days             INTEGER,
    learned_n                INTEGER,
    learned_at               TEXT,
    learned_confidence       REAL,
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS fill_levels (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    fill_percent REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'purchase', 'onboarding')),
    UNIQUE (user_id, date, source)
);

CREATE TABLE IF NOT EXISTS reference_prices (
    date         TEXT PRIMARY KEY,
    source       TEXT NOT NULL CHECK (source IN ('tecson', 'fallback', 'import', 'manual')),
    ct_per_liter REAL NOT NULL,
    brent_usd    REAL,
    eurusd       REAL,
    fetched_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS co2_steps (
    id           INTEGER PRIMARY KEY,
    valid_from   TEXT NOT NULL UNIQUE,
    eur_per_t    REAL NOT NULL,                 -- Untergrenze bzw. Festpreis
    eur_per_t_max REAL,                         -- Obergrenze bei Korridor, sonst NULL
    status       TEXT NOT NULL DEFAULT 'fixed' CHECK (status IN ('fixed', 'draft', 'market')),
    note         TEXT,
    source_url   TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    region       TEXT NOT NULL,
    date         TEXT NOT NULL,
    ct_per_liter REAL NOT NULL,
    liter        INTEGER NOT NULL,
    dealer       TEXT,
    portal       TEXT,
    is_outlier   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS region_offsets (
    region     TEXT NOT NULL,
    date       TEXT NOT NULL,
    offset_ct  REAL NOT NULL,
    n_offers   INTEGER NOT NULL,
    PRIMARY KEY (region, date)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id                   INTEGER PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date                 TEXT NOT NULL,
    verdict              TEXT NOT NULL CHECK (verdict IN ('buy_now', 'buy_before_step', 'wait', 'watch')),
    rule                 TEXT NOT NULL,
    reason               TEXT NOT NULL,
    personal_ct          REAL NOT NULL,
    percentile_365       REAL,
    percentile_730       REAL,
    season_deviation_ct  REAL,
    range_days           REAL NOT NULL,
    latest_order_date    TEXT NOT NULL,
    next_step_date       TEXT,
    next_step_ct         REAL,
    need_liter           INTEGER NOT NULL,
    inputs_json          TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (user_id, date)
);

CREATE TABLE IF NOT EXISTS purchases (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    liter        INTEGER NOT NULL,
    ct_per_liter REAL NOT NULL,
    dealer       TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS news_digests (
    date        TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    items_json  TEXT NOT NULL,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    duration_ms INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS rule_change_hints (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    title      TEXT NOT NULL,
    quote      TEXT NOT NULL,
    url        TEXT,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'dismissed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (date, url)
);

CREATE TABLE IF NOT EXISTS news_items (
    id         INTEGER PRIMARY KEY,
    url        TEXT NOT NULL UNIQUE,
    feed       TEXT NOT NULL,
    title      TEXT NOT NULL,
    published  TEXT,
    summary    TEXT,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    relevant   INTEGER
);

CREATE TABLE IF NOT EXISTS portal_offers (
    id            INTEGER PRIMARY KEY,
    date          TEXT NOT NULL,
    plz           TEXT NOT NULL,
    liter         INTEGER NOT NULL,
    portal        TEXT NOT NULL,
    dealer        TEXT,
    ct_per_liter  REAL NOT NULL,          -- brutto, inkl. Lieferung, für diese Menge
    total_eur     REAL,
    delivery_days INTEGER,
    url           TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_portal_offers_lookup ON portal_offers(plz, liter, date);

CREATE TABLE IF NOT EXISTS reminders_sent (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,                  -- z. B. 'fill_level'
    date     TEXT NOT NULL,
    UNIQUE (user_id, kind, date)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS notifications_sent (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date     TEXT NOT NULL,
    verdict  TEXT NOT NULL,
    channel  TEXT NOT NULL,
    UNIQUE (user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_offers_region_date ON offers(region, date);
CREATE INDEX IF NOT EXISTS idx_recommendations_user_date ON recommendations(user_id, date);
CREATE INDEX IF NOT EXISTS idx_fill_levels_user_date ON fill_levels(user_id, date);
