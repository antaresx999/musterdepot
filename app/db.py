"""SQLite-Datenbank: Schema, Migrationen und Hilfsfunktionen."""
import sqlite3
from contextlib import contextmanager

from . import config

# Version 1: Grundschema
V1 = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login    TEXT
);

-- Ein Depot gehört einem Benutzer und hat eine eigene Währung und Gebühren
CREATE TABLE portfolios (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL,
    cash        REAL NOT NULL DEFAULT 0,
    fee_fixed   REAL NOT NULL DEFAULT 0,   -- fixe Gebühr pro Auftrag (Depotwährung)
    fee_pct     REAL NOT NULL DEFAULT 0,   -- Prozent vom Kurswert
    fee_min     REAL NOT NULL DEFAULT 0,   -- Mindestgebühr
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_portfolios_user ON portfolios(user_id);

-- Handelbare Titel. provider: yahoo, coingecko oder manual
CREATE TABLE instruments (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    symbol      TEXT NOT NULL,             -- Yahoo-Symbol bzw. CoinGecko-ID
    ticker      TEXT NOT NULL DEFAULT '',  -- Anzeige-Kürzel
    name        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'stock' CHECK (kind IN ('stock','etf','fund','crypto','other')),
    currency    TEXT NOT NULL DEFAULT 'USD',
    exchange    TEXT NOT NULL DEFAULT '',
    price       REAL,
    prev_close  REAL,
    price_time  TEXT,                      -- Zeitpunkt des Kurses (UTC)
    updated_at  TEXT,                      -- letzte erfolgreiche Abfrage (UTC)
    last_error  TEXT NOT NULL DEFAULT '',
    history_at  TEXT,                      -- letzte Aktualisierung des Kursverlaufs
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, symbol)
);

CREATE TABLE price_history (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    day           TEXT NOT NULL,
    close         REAL NOT NULL,
    PRIMARY KEY (instrument_id, day)
);

CREATE TABLE holdings (
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    quantity      REAL NOT NULL,
    cost          REAL NOT NULL,          -- Einstandswert inkl. Gebühren (Depotwährung)
    PRIMARY KEY (portfolio_id, instrument_id)
);

-- Aufträge: market wird sofort ausgeführt, limit/stop warten auf den Kurs
CREATE TABLE orders (
    id            INTEGER PRIMARY KEY,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
    order_type    TEXT NOT NULL CHECK (order_type IN ('market','limit','stop')),
    quantity      REAL NOT NULL,
    limit_price   REAL,
    stop_price    REAL,
    valid_until   TEXT,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','filled','cancelled','rejected','expired')),
    message       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at     TEXT
);
CREATE INDEX idx_orders_status ON orders(status);

-- Buchungen. amount = Wirkung auf das Bargeld (Depotwährung, mit Vorzeichen)
CREATE TABLE transactions (
    id            INTEGER PRIMARY KEY,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id INTEGER REFERENCES instruments(id),
    order_id      INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('deposit','withdraw','buy','sell','dividend')),
    quantity      REAL,
    price         REAL,                   -- Kurs in Titelwährung
    fx            REAL,                   -- Umrechnung Titelwährung -> Depotwährung
    fee           REAL NOT NULL DEFAULT 0,
    tax           REAL NOT NULL DEFAULT 0,
    amount        REAL NOT NULL,
    realized      REAL,                   -- realisierter Gewinn/Verlust bei Verkäufen
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_tx_portfolio ON transactions(portfolio_id, created_at);

-- Tageswerte für die Depotwert-Grafik
CREATE TABLE snapshots (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    value        REAL NOT NULL,
    invested     REAL NOT NULL,           -- eingezahltes Kapital (netto)
    PRIMARY KEY (portfolio_id, day)
);

CREATE TABLE fx_rates (
    pair       TEXT PRIMARY KEY,          -- z. B. USDCHF
    rate       REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE watchlist (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, instrument_id)
);
"""

MIGRATIONS = [V1]


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get():
    """Verbindung mit Transaktion: alles oder nichts."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except BaseException:
        conn.close()
        raise
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript("BEGIN;" + script + ";PRAGMA user_version = {};COMMIT;".format(i))
    finally:
        conn.close()


def one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def all_(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
