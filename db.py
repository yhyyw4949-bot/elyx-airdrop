"""SQLite storage for the airdrop campaign.

Design notes:
  - `users.points` is the spendable balance; every change is recorded in
    `points_ledger` with a reason so balances are always traceable.
  - Balances never go negative: point deductions are guarded in SQL.
  - Multi-step writes run inside BEGIN IMMEDIATE transactions so concurrent
    requests cannot double-spend or double-award.
  - Referrals are recorded once per invited user (PRIMARY KEY) and credited
    once (`credited` flag flipped inside the same transaction as the award).
  - Withdrawals lock points at request time; reject/cancel releases them.
    One pending withdrawal per user (partial unique index).
"""

import sqlite3
from datetime import datetime, timezone

from config import (
    CHANNEL_USERNAME,
    DB_PATH,
    GROUP_USERNAME,
    POINTS_PER_REFERRAL,
    POINTS_PER_TOKEN,
    X_PROFILE_URL,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    referrer_id       INTEGER,
    points            INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    wallet            TEXT,
    whitelisted       INTEGER NOT NULL DEFAULT 0,
    banned            INTEGER NOT NULL DEFAULT 0,
    joined_channel    INTEGER NOT NULL DEFAULT 0,  -- legacy, mirrors the channel task
    followed_x        INTEGER NOT NULL DEFAULT 0,  -- legacy, mirrors the x task
    created_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users (referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_whitelisted ON users (whitelisted);
CREATE INDEX IF NOT EXISTS idx_users_points ON users (points DESC);

CREATE TABLE IF NOT EXISTS referrals (
    referee_id        INTEGER PRIMARY KEY,
    referrer_id       INTEGER NOT NULL,
    credited          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_id);

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key               TEXT UNIQUE NOT NULL,
    title             TEXT NOT NULL,
    kind              TEXT NOT NULL,           -- channel | x | wallet | custom
    url               TEXT,
    points            INTEGER NOT NULL DEFAULT 0,
    verification      TEXT NOT NULL DEFAULT 'confirm',  -- channel | x_api | confirm | wallet
    max_participants  INTEGER,                 -- NULL = unlimited
    active            INTEGER NOT NULL DEFAULT 1,
    sort_order        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS task_completions (
    user_id           INTEGER NOT NULL,
    task_id           INTEGER NOT NULL,
    created_at        TEXT,
    PRIMARY KEY (user_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_completions_task ON task_completions (task_id);

CREATE TABLE IF NOT EXISTS points_ledger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    delta             INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    ref               TEXT,
    balance_after     INTEGER NOT NULL,
    created_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON points_ledger (user_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    tokens            INTEGER NOT NULL,
    points_locked     INTEGER NOT NULL,
    wallet            TEXT NOT NULL,
    network           TEXT NOT NULL DEFAULT 'TON',
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|completed|cancelled
    reason            TEXT,
    created_at        TEXT,
    processed_at      TEXT,
    processed_by      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_one_pending
    ON withdrawals (user_id) WHERE status IN ('pending', 'approved');
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals (status);

CREATE TABLE IF NOT EXISTS admin_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id          INTEGER NOT NULL,
    action            TEXT NOT NULL,
    details           TEXT,
    created_at        TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key               TEXT PRIMARY KEY,
    value             TEXT
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # Migrations for databases created by older versions.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "whitelisted" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN whitelisted INTEGER NOT NULL DEFAULT 0")
        if "banned" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        if "joined_channel" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN joined_channel INTEGER NOT NULL DEFAULT 0")
        if "followed_x" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN followed_x INTEGER NOT NULL DEFAULT 0")
        # Move the phase setting into app_state if it lived in the old table name.
        seed_default_tasks(conn)


def seed_default_tasks(conn: sqlite3.Connection) -> None:
    """Create the standard tasks once, from the configured channel/group/X."""
    if conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]:
        return
    now = _now()
    defaults = [
        ("channel", "Join the Telegram channel", "channel",
         f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}" if CHANNEL_USERNAME.startswith("@") else None,
         10, "channel", 1),
    ]
    if GROUP_USERNAME:
        defaults.append(
            ("group", "Join the Telegram group", "channel",
             f"https://t.me/{GROUP_USERNAME.lstrip('@')}" if GROUP_USERNAME.startswith("@") else None,
             10, "channel", 2),
        )
    defaults.append(("x", "Follow us on X", "x", X_PROFILE_URL, 10, "x_api", 3))
    defaults.append(("wallet", "Connect your TON wallet", "wallet", None, 10, "wallet", 4))
    for key, title, kind, url, points, verification, order in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO tasks (key, title, kind, url, points, verification,"
            " sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, title, kind, url, points, verification, order, now),
        )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def register_user(user_id: int, username: str | None, first_name: str | None,
                  referrer_id: int | None, whitelisted: bool = False) -> bool:
    """Insert the user if new (recording the referrer). Returns True for a new user."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id,"
            " whitelisted, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id, int(whitelisted), _now()),
        )
        is_new = cur.rowcount == 1
        if is_new:
            if referrer_id and referrer_id != user_id \
                    and conn.execute("SELECT 1 FROM users WHERE user_id = ?",
                                     (referrer_id,)).fetchone():
                conn.execute(
                    "INSERT OR IGNORE INTO referrals (referee_id, referrer_id, created_at)"
                    " VALUES (?, ?, ?)",
                    (user_id, referrer_id, _now()),
                )
        else:
            conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
        return is_new


def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def is_banned(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["banned"])


def set_flag(user_id: int, field: str, value) -> None:
    assert field in {"joined_channel", "followed_x", "wallet", "whitelisted", "banned"}
    # joined_channel / followed_x are legacy columns kept for old exports.
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


# --------------------------------------------------------------------------
# Points ledger
# --------------------------------------------------------------------------

def _apply_points(conn: sqlite3.Connection, user_id: int, delta: int,
                  reason: str, ref: str | None = None) -> int:
    """Change a balance inside the caller's transaction. Returns new balance."""
    cur = conn.execute(
        "UPDATE users SET points = points + ? WHERE user_id = ? AND points + ? >= 0",
        (delta, user_id, delta),
    )
    if cur.rowcount != 1:
        raise ValueError("Insufficient balance")
    balance = conn.execute(
        "SELECT points FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()["points"]
    conn.execute(
        "INSERT INTO points_ledger (user_id, delta, reason, ref, balance_after, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, delta, reason, ref, balance, _now()),
    )
    return balance


def add_points(user_id: int, points: int, reason: str = "manual",
               ref: str | None = None) -> int:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _apply_points(conn, user_id, points, reason, ref)


def get_balance(user_id: int) -> int:
    row = get_user(user_id)
    return row["points"] if row else 0


def ledger_for(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM points_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# --------------------------------------------------------------------------
# Referrals
# --------------------------------------------------------------------------

def referral_count(user_id: int, credited_only: bool = False) -> int:
    with db() as conn:
        q = "SELECT COUNT(*) c FROM referrals WHERE referrer_id = ?"
        if credited_only:
            q += " AND credited = 1"
        return conn.execute(q, (user_id,)).fetchone()["c"]


def referral_list(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT r.*, u.username, u.first_name FROM referrals r"
            " LEFT JOIN users u ON u.user_id = r.referee_id"
            " WHERE r.referrer_id = ? ORDER BY r.created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def credit_referral_if_due(referee_id: int, points: int) -> tuple[int | None, int]:
    """Credit the referrer for this referee, exactly once.

    Call at the moment the referral becomes valid (at registration, or after
    the referee verifies the channel when REFERRAL_REQUIRES_VERIFIED is on).
    Returns (referrer_id, awarded_points), or (None, 0) when nothing was due.
    """
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM referrals WHERE referee_id = ?", (referee_id,)
        ).fetchone()
        if not row or row["credited"]:
            return None, 0
        referrer_id = row["referrer_id"]
        conn.execute("UPDATE referrals SET credited = 1 WHERE referee_id = ?", (referee_id,))
        _apply_points(conn, referrer_id, points, "referral", ref=str(referee_id))
        return referrer_id, points


def referral_history(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT r.referee_id, r.credited, r.created_at, u.username, u.first_name"
            " FROM referrals r LEFT JOIN users u ON u.user_id = r.referee_id"
            " WHERE r.referrer_id = ? ORDER BY r.created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def referral_leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT u.user_id, u.username, u.first_name, COUNT(*) c"
            " FROM referrals r JOIN users u ON u.user_id = r.referrer_id"
            " WHERE r.credited = 1"
            " GROUP BY r.referrer_id ORDER BY c DESC LIMIT ?",
            (limit,),
        ).fetchall()


def points_leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT user_id, username, first_name, points FROM users"
            " WHERE banned = 0 ORDER BY points DESC LIMIT ?",
            (limit,),
        ).fetchall()


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def list_tasks(active_only: bool = True) -> list[sqlite3.Row]:
    with db() as conn:
        q = "SELECT * FROM tasks"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY sort_order, id"
        return conn.execute(q).fetchall()


def get_task(task_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_task_by_key(key: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()


def task_completions_for(user_id: int) -> set[int]:
    with db() as conn:
        return {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM task_completions WHERE user_id = ?", (user_id,))}


def task_completion_count(task_id: int) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM task_completions WHERE task_id = ?", (task_id,)
        ).fetchone()["c"]


def complete_task(user_id: int, task_id: int) -> int:
    """Record a task completion and award its points, atomically.

    Raises ValueError('already completed') on a duplicate claim and
    ValueError('task full') when max_participants is reached.
    Returns the new balance.
    """
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND active = 1",
                            (task_id,)).fetchone()
        if not task:
            raise ValueError("unknown task")
        if conn.execute("SELECT 1 FROM task_completions WHERE user_id = ? AND task_id = ?",
                        (user_id, task_id)).fetchone():
            raise ValueError("already completed")
        if task["max_participants"] is not None:
            count = conn.execute(
                "SELECT COUNT(*) c FROM task_completions WHERE task_id = ?", (task_id,)
            ).fetchone()["c"]
            if count >= task["max_participants"]:
                raise ValueError("task full")
        conn.execute(
            "INSERT INTO task_completions (user_id, task_id, created_at) VALUES (?, ?, ?)",
            (user_id, task_id, _now()),
        )
        balance = _apply_points(conn, user_id, task["points"], f"task:{task['key']}")
        return balance


def upsert_task(task_id: int | None, key: str, title: str, kind: str, url: str | None,
                points: int, verification: str, max_participants: int | None,
                active: bool, sort_order: int) -> int:
    with db() as conn:
        if task_id:
            conn.execute(
                "UPDATE tasks SET key = ?, title = ?, kind = ?, url = ?, points = ?,"
                " verification = ?, max_participants = ?, active = ?, sort_order = ?"
                " WHERE id = ?",
                (key, title, kind, url, points, verification, max_participants,
                 int(active), sort_order, task_id),
            )
            return task_id
        cur = conn.execute(
            "INSERT INTO tasks (key, title, kind, url, points, verification,"
            " max_participants, active, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, title, kind, url, points, verification, max_participants,
             int(active), sort_order, _now()),
        )
        return cur.lastrowid


def delete_task(task_id: int) -> bool:
    """Delete a task only when nobody completed it; otherwise disable it."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM task_completions WHERE task_id = ? LIMIT 1",
                        (task_id,)).fetchone():
            conn.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))
            return False
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return True


# --------------------------------------------------------------------------
# Withdrawals
# --------------------------------------------------------------------------

def request_withdrawal(user_id: int, tokens: int, wallet: str, network: str,
                       min_tokens: int) -> sqlite3.Row:
    """Create a pending withdrawal, locking the points behind the tokens.

    Raises ValueError with a user-facing reason on any rule violation.
    """
    points_needed = tokens * POINTS_PER_TOKEN
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            raise ValueError("Register first")
        if user["banned"]:
            raise ValueError("Account is banned")
        if tokens < min_tokens:
            raise ValueError(f"Minimum withdrawal is {min_tokens} tokens")
        if user["points"] < points_needed:
            raise ValueError("Not enough points for that amount")
        try:
            _apply_points(conn, user_id, -points_needed, "withdrawal_lock")
        except ValueError:
            raise ValueError("Not enough points for that amount")
        try:
            cur = conn.execute(
                "INSERT INTO withdrawals (user_id, tokens, points_locked, wallet, network,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, tokens, points_needed, wallet, network, _now()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("You already have a withdrawal in progress")
        return conn.execute("SELECT * FROM withdrawals WHERE id = ?",
                            (cur.lastrowid,)).fetchone()


def decide_withdrawal(withdrawal_id: int, action: str, admin_id: int,
                      reason: str | None = None) -> sqlite3.Row:
    """approve | reject | complete a withdrawal. Idempotent per status.

    approve: pending -> approved (points stay locked)
    reject:  pending|approved -> rejected (points released)
    complete: approved -> completed (payout sent by the admin off-chain)
    """
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        w = conn.execute("SELECT * FROM withdrawals WHERE id = ?",
                         (withdrawal_id,)).fetchone()
        if not w:
            raise ValueError("No such withdrawal")
        transitions = {
            "approve": {"pending"},
            "reject": {"pending", "approved"},
            "complete": {"approved"},
        }
        if action not in transitions:
            raise ValueError("Unknown action")
        if w["status"] not in transitions[action]:
            raise ValueError(f"Cannot {action} a {w['status']} withdrawal")
        if action == "reject":
            _apply_points(conn, w["user_id"], w["points_locked"],
                          "withdrawal_release", ref=str(withdrawal_id))
        new_status = {"approve": "approved", "reject": "rejected",
                      "complete": "completed"}[action]
        conn.execute(
            "UPDATE withdrawals SET status = ?, reason = ?, processed_at = ?,"
            " processed_by = ? WHERE id = ?",
            (new_status, reason, _now(), admin_id, withdrawal_id),
        )
        return conn.execute("SELECT * FROM withdrawals WHERE id = ?",
                            (withdrawal_id,)).fetchone()


def cancel_withdrawal(withdrawal_id: int, user_id: int) -> sqlite3.Row:
    """User cancels their own pending withdrawal; points are released."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        w = conn.execute("SELECT * FROM withdrawals WHERE id = ? AND user_id = ?",
                         (withdrawal_id, user_id)).fetchone()
        if not w:
            raise ValueError("No such withdrawal")
        if w["status"] != "pending":
            raise ValueError("Only pending withdrawals can be cancelled")
        _apply_points(conn, user_id, w["points_locked"],
                      "withdrawal_release", ref=str(withdrawal_id))
        conn.execute(
            "UPDATE withdrawals SET status = 'cancelled', processed_at = ? WHERE id = ?",
            (_now(), withdrawal_id),
        )
        return conn.execute("SELECT * FROM withdrawals WHERE id = ?",
                            (withdrawal_id,)).fetchone()


def list_withdrawals(status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    with db() as conn:
        q = ("SELECT w.*, u.username, u.first_name FROM withdrawals w"
             " LEFT JOIN users u ON u.user_id = w.user_id")
        args: tuple = ()
        if status:
            q += " WHERE w.status = ?"
            args = (status,)
        q += " ORDER BY w.id DESC LIMIT ?"
        args += (limit,)
        return conn.execute(q, args).fetchall()


def withdrawals_for(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user_id,),
        ).fetchall()


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def log_admin_action(admin_id: int, action: str, details: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_actions (admin_id, action, details, created_at)"
            " VALUES (?, ?, ?, ?)",
            (admin_id, action, details, _now()),
        )


def admin_actions(limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM admin_actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# --------------------------------------------------------------------------
# Phases, stats, misc
# --------------------------------------------------------------------------

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
            "SELECT * FROM users WHERE whitelisted = 0 AND banned = 0"
            " ORDER BY created_at LIMIT 100"
        ).fetchall()


def tokens_for(points: int) -> int:
    """Convert points to ELYX at the configured rate."""
    return points // POINTS_PER_TOKEN if POINTS_PER_TOKEN > 0 else 0


def stats() -> dict:
    with db() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()["c"]
        return {
            "total": one("SELECT COUNT(*) c FROM users"),
            "whitelisted": one("SELECT COUNT(*) c FROM users WHERE whitelisted = 1"),
            "banned": one("SELECT COUNT(*) c FROM users WHERE banned = 1"),
            "wallets": one("SELECT COUNT(*) c FROM users WHERE wallet IS NOT NULL"),
            "referred": one("SELECT COUNT(*) c FROM users WHERE referrer_id IS NOT NULL"),
            "points_total": one("SELECT COALESCE(SUM(points),0) c FROM users"),
            "new_today": one("SELECT COUNT(*) c FROM users WHERE created_at >= ?",
                             datetime.now(timezone.utc).date().isoformat()),
            "pending_withdrawals": one(
                "SELECT COUNT(*) c FROM withdrawals WHERE status = 'pending'"),
            "completed_withdrawals": one(
                "SELECT COUNT(*) c FROM withdrawals WHERE status = 'completed'"),
        }


def all_users(limit: int = 10000) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()


def search_users(query: str, limit: int = 50) -> list[sqlite3.Row]:
    with db() as conn:
        if query.isdigit():
            return conn.execute(
                "SELECT * FROM users WHERE user_id = ? OR CAST(user_id AS TEXT) LIKE ?"
                " ORDER BY created_at LIMIT ?",
                (int(query), f"{query}%", limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ?"
            " ORDER BY created_at LIMIT ?",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
