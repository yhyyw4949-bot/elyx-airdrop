"""SQLite storage for the airdrop campaign."""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, POINTS_PER_TOKEN

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    referrer_id       INTEGER,
    points            INTEGER NOT NULL DEFAULT 0,
    wallet            TEXT,
    whitelisted       INTEGER NOT NULL DEFAULT 0,
    joined_channel    INTEGER NOT NULL DEFAULT 0,
    followed_x        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # Migration for databases created before phases existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "whitelisted" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN whitelisted INTEGER NOT NULL DEFAULT 0")


def register_user(user_id: int, username: str | None, first_name: str | None,
                  referrer_id: int | None, whitelisted: bool = False) -> bool:
    """Insert the user if new (recording the referrer). Returns True for a new user."""
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id,"
            " whitelisted, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id, int(whitelisted),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (username, first_name, user_id),
        )
        return cur.rowcount == 1


def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def set_flag(user_id: int, field: str, value) -> None:
    assert field in {"joined_channel", "followed_x", "wallet", "points", "whitelisted"}
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


def add_points(user_id: int, points: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))


def referral_count(user_id: int) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referrer_id = ?", (user_id,)
        ).fetchone()["c"]


def get_phase() -> int:
    with db() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = 'phase'").fetchone()
        return int(row["value"]) if row else 1


def set_phase(phase: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('phase', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(phase),),
        )


def pending_whitelist() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE whitelisted = 0 ORDER BY created_at LIMIT 100"
        ).fetchall()


def tokens_for(points: int) -> int:
    """Convert points to ELYX at the configured rate."""
    return points // POINTS_PER_TOKEN if POINTS_PER_TOKEN > 0 else 0


def stats() -> dict:
    with db() as conn:
        one = lambda q: conn.execute(q).fetchone()["c"]
        return {
            "total": one("SELECT COUNT(*) c FROM users"),
            "whitelisted": one("SELECT COUNT(*) c FROM users WHERE whitelisted = 1"),
            "joined": one("SELECT COUNT(*) c FROM users WHERE joined_channel = 1"),
            "wallets": one("SELECT COUNT(*) c FROM users WHERE wallet IS NOT NULL"),
            "referred": one("SELECT COUNT(*) c FROM users WHERE referrer_id IS NOT NULL"),
        }


def all_users() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT user_id, username, first_name, wallet, whitelisted, points, referrer_id,"
            " joined_channel, followed_x, created_at FROM users ORDER BY created_at"
        ).fetchall()
