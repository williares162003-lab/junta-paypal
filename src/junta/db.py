"""SQLite storage. The schema itself enforces the money rules that must never break."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    contribution_cents  INTEGER NOT NULL CHECK (contribution_cents > 0),
    currency            TEXT NOT NULL DEFAULT 'USD',
    organizer_email     TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES groups(id),
    name          TEXT NOT NULL,
    paypal_email  TEXT NOT NULL,
    turn          INTEGER NOT NULL CHECK (turn > 0),   -- round in which this member receives the pot
    UNIQUE (group_id, turn),
    UNIQUE (group_id, paypal_email)
);

-- One round per turn. The recipient is copied here so later turn swaps never rewrite history.
CREATE TABLE IF NOT EXISTS rounds (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES groups(id),
    number        INTEGER NOT NULL CHECK (number > 0),
    recipient_id  INTEGER NOT NULL REFERENCES members(id),
    status        TEXT NOT NULL DEFAULT 'COLLECTING' CHECK (status IN ('COLLECTING', 'PAYING_OUT', 'PAID_OUT')),
    UNIQUE (group_id, number)
);

-- Every member, including the recipient, contributes once per round.
CREATE TABLE IF NOT EXISTS contributions (
    id                 INTEGER PRIMARY KEY,
    round_id           INTEGER NOT NULL REFERENCES rounds(id),
    member_id          INTEGER NOT NULL REFERENCES members(id),
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    status             TEXT NOT NULL DEFAULT 'DUE'
                       CHECK (status IN ('DUE', 'ORDER_CREATED', 'CAPTURED', 'FAILED')),
    paypal_order_id    TEXT UNIQUE,
    order_attempts     INTEGER NOT NULL DEFAULT 0,   -- unapproved PayPal orders expire; a new one is created
    paypal_capture_id  TEXT UNIQUE,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (round_id, member_id)
);

-- At most one payout per round, ever (UNIQUE round_id). The row is reserved before calling PayPal.
CREATE TABLE IF NOT EXISTS payouts (
    id               INTEGER PRIMARY KEY,
    round_id         INTEGER NOT NULL UNIQUE REFERENCES rounds(id),
    recipient_email  TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    sender_batch_id  TEXT NOT NULL UNIQUE,
    paypal_batch_id  TEXT UNIQUE,
    status           TEXT NOT NULL DEFAULT 'RESERVED'
                     CHECK (status IN ('RESERVED', 'PENDING', 'PROCESSING', 'SUCCESS', 'FAILED', 'UNKNOWN')),
    approved_by      TEXT NOT NULL,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Group chat. member_id NULL = the organizer bot. source = 'ai' or 'rules' (how the bot understood/replied).
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES groups(id),
    member_id  INTEGER REFERENCES members(id),
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'voice')),  -- voice = Whisper transcript
    intent     TEXT,
    source     TEXT,
    at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What each member asked for. Only EARLY/LATER members can be moved by a swap proposal.
CREATE TABLE IF NOT EXISTS preferences (
    member_id   INTEGER PRIMARY KEY REFERENCES members(id),
    preference  TEXT NOT NULL CHECK (preference IN ('EARLY', 'LATER', 'NEUTRAL')),
    urgency     INTEGER NOT NULL DEFAULT 0 CHECK (urgency BETWEEN 0 AND 3),
    reason      TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A turn-swap proposal is applied only when every affected member accepts it.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL REFERENCES groups(id),
    changes     TEXT NOT NULL,     -- JSON {member_id: [old_turn, new_turn]}
    status      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPLIED', 'REJECTED', 'STALE')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS proposal_votes (
    proposal_id  INTEGER NOT NULL REFERENCES proposals(id),
    member_id    INTEGER NOT NULL REFERENCES members(id),
    accept       INTEGER NOT NULL CHECK (accept IN (0, 1)),
    at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (proposal_id, member_id)
);

-- Append-only history of everything that happened, shown to the group.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    group_id  INTEGER NOT NULL REFERENCES groups(id),
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path) -> sqlite3.Connection:
    """Open the database with foreign keys on and rows readable by column name."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions via BEGIN IMMEDIATE
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after a database was created (CREATE TABLE IF NOT EXISTS does not add them)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")


def log_event(conn, group_id, kind, **detail):
    conn.execute("INSERT INTO events (group_id, kind, detail) VALUES (?, ?, ?)",
                 (group_id, kind, json.dumps(detail, ensure_ascii=False)))
