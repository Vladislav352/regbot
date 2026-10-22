"""SQLite-хранилище регуляторного бота.

Все данные — в одном файле data/regbot.db. Полнотекстовый поиск — FTS5
(unicode61, префиксные запросы вместо стемминга).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "regbot.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    role        TEXT NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    is_allowed  INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    direction   TEXT NOT NULL,                  -- 'in' | 'out'
    text        TEXT NOT NULL,
    meta        TEXT,                           -- JSON: engine, chunks, timing
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file         TEXT UNIQUE NOT NULL,
    title        TEXT NOT NULL,
    doc_type     TEXT,
    number       TEXT,
    doc_date     TEXT,
    edition_date TEXT,
    source_url   TEXT,
    category     TEXT,                          -- med | cosmetics | customs | review
    volatile     INTEGER NOT NULL DEFAULT 0,    -- 1 = быстро устаревает
    ingested_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    heading  TEXT,
    text     TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading)
    VALUES ('delete', old.id, old.text, old.heading);
END;

CREATE TABLE IF NOT EXISTS registry_cache (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    registry   TEXT NOT NULL,                   -- 'rzn' | 'fsa'
    query      TEXT NOT NULL,
    response   TEXT NOT NULL,                   -- JSON
    fetched_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_registry_cache ON registry_cache(registry, query);

CREATE TABLE IF NOT EXISTS monitor_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    guid       TEXT UNIQUE NOT NULL,
    title      TEXT,
    url        TEXT,
    seen_at    TEXT DEFAULT (datetime('now')),
    digested   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    items      INTEGER NOT NULL,
    sent_to    TEXT,                            -- JSON list of tg_id
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER REFERENCES messages(id),
    tg_id      INTEGER NOT NULL,
    verdict    TEXT NOT NULL,                   -- 'up' | 'down'
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# --- users -----------------------------------------------------------------

def is_allowed(con: sqlite3.Connection, tg_id: int) -> bool:
    row = con.execute("SELECT is_allowed FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return bool(row and row["is_allowed"])


def is_admin(con: sqlite3.Connection, tg_id: int) -> bool:
    row = con.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return bool(row and row["role"] == "admin")


def upsert_user(con: sqlite3.Connection, tg_id: int, username: str | None,
                full_name: str | None, *, allowed: bool | None = None,
                role: str | None = None) -> None:
    con.execute(
        "INSERT INTO users(tg_id, username, full_name) VALUES(?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, "
        "full_name=excluded.full_name",
        (tg_id, username, full_name),
    )
    if allowed is not None:
        con.execute("UPDATE users SET is_allowed=? WHERE tg_id=?", (int(allowed), tg_id))
    if role is not None:
        con.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))
    con.commit()


def allowed_user_ids(con: sqlite3.Connection) -> list[int]:
    return [r["tg_id"] for r in con.execute("SELECT tg_id FROM users WHERE is_allowed=1")]


# --- messages / feedback ---------------------------------------------------

def log_message(con: sqlite3.Connection, tg_id: int, direction: str, text: str,
                meta: dict | None = None) -> int:
    cur = con.execute(
        "INSERT INTO messages(tg_id, direction, text, meta) VALUES(?,?,?,?)",
        (tg_id, direction, text, json.dumps(meta, ensure_ascii=False) if meta else None),
    )
    con.commit()
    return cur.lastrowid


def add_feedback(con: sqlite3.Connection, message_id: int, tg_id: int, verdict: str) -> None:
    con.execute("INSERT INTO feedback(message_id, tg_id, verdict) VALUES(?,?,?)",
                (message_id, tg_id, verdict))
    con.commit()


# --- registry cache --------------------------------------------------------

CACHE_TTL_SEC = 24 * 3600


def cache_get(con: sqlite3.Connection, registry: str, query: str) -> dict | None:
    row = con.execute(
        "SELECT response, fetched_at FROM registry_cache WHERE registry=? AND query=? "
        "ORDER BY id DESC LIMIT 1", (registry, query)).fetchone()
    if not row:
        return None
    fetched = time.mktime(time.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S"))
    if time.time() - time.timezone - fetched > CACHE_TTL_SEC:
        return None
    return json.loads(row["response"])


def cache_put(con: sqlite3.Connection, registry: str, query: str, response: dict) -> None:
    con.execute("INSERT INTO registry_cache(registry, query, response) VALUES(?,?,?)",
                (registry, query, json.dumps(response, ensure_ascii=False)))
    con.commit()


if __name__ == "__main__":
    init()
    print(f"OK: схема создана в {DB_PATH}")
