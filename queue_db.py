"""
queue_db.py — Render side TEMP SQLite (queue + cache only, NEVER permanent)
---------------------------------------------------------------------------
Stores three things on Render:

  1. event_queue  — events waiting to be synced to the laptop (master DB)
  2. ban_cache    — so banned users are blocked even when laptop is offline
  3. user_cache   — basic profile + counters so the bot works offline / instantly

On a Render restart this DB is wiped — that is ACCEPTABLE by design.
The laptop holds the permanent data; the bot re-fetches fresh state from
the laptop on startup (see sync_engine.startup_sync()).

Everything here is plain stdlib sqlite3 — no extra deps.
"""

import sqlite3
import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# On Render free tier both /tmp and cwd are wiped on restart — that's fine.
QUEUE_DB = os.getenv("QUEUE_DB_PATH", "render_queue.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn():
    parent = os.path.dirname(QUEUE_DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(QUEUE_DB, timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # faster, safe enough with WAL
    conn.row_factory = sqlite3.Row
    return conn


def init_queue_db():
    with get_conn() as c:
        c.executescript("""
            -- events waiting to be sent to the laptop
            CREATE TABLE IF NOT EXISTS event_queue (
                event_id    TEXT PRIMARY KEY,        -- UUID, unique forever
                user_id     INTEGER NOT NULL,
                event_type  TEXT NOT NULL,           -- REPORT_USER, REFERRAL_INCREMENT, ...
                payload     TEXT NOT NULL,           -- JSON
                status      TEXT DEFAULT 'pending',  -- pending | syncing
                created_at  TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                last_retry  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_eq_status ON event_queue(status, created_at);

            -- ban cache: checked on every action, even when laptop offline
            CREATE TABLE IF NOT EXISTS ban_cache (
                user_id     INTEGER PRIMARY KEY,
                ban_until   TEXT,                    -- NULL with permanent=1 => forever
                permanent   INTEGER DEFAULT 0,
                reason      TEXT,
                cached_at   TEXT NOT NULL
            );

            -- basic profile cache so the bot works instantly / offline
            CREATE TABLE IF NOT EXISTS user_cache (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                gender         TEXT,
                age            INTEGER,
                country        TEXT,
                country_flag   TEXT,
                is_premium     INTEGER DEFAULT 0,
                premium_until  TEXT,
                referral_code  TEXT,
                referral_count INTEGER DEFAULT 0,
                messages_sent  INTEGER DEFAULT 0,
                media_approved INTEGER DEFAULT 0,
                cached_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_uc_refcode  ON user_cache(referral_code);
            CREATE INDEX IF NOT EXISTS idx_uc_username ON user_cache(username);
        """)
        c.commit()
    logger.info(f"[QUEUE_DB] Initialized at {QUEUE_DB}")


# ─── EVENT QUEUE ────────────────────────────────────────────────────────────

def enqueue_event(event_id: str, user_id: int, event_type: str, payload: dict):
    """Push one event into the queue. Duplicate event_id is silently ignored."""
    with get_conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO event_queue
               (event_id, user_id, event_type, payload, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (event_id, user_id, event_type, json.dumps(payload), _now())
        )
        c.commit()


def fetch_pending_batch(batch_size: int = 50):
    """Fetch up to batch_size pending events, mark them 'syncing'."""
    with get_conn() as c:
        rows = c.execute(
            """SELECT event_id, user_id, event_type, payload, created_at, retry_count
               FROM event_queue
               WHERE status='pending'
               ORDER BY created_at ASC
               LIMIT ?""",
            (batch_size,)
        ).fetchall()
        if not rows:
            return []
        ids = [r["event_id"] for r in rows]
        ph = ",".join("?" * len(ids))
        c.execute(f"UPDATE event_queue SET status='syncing' WHERE event_id IN ({ph})", ids)
        c.commit()
    return [dict(r) for r in rows]


def mark_synced(event_ids: list):
    """ACK received from laptop — delete these events from the queue."""
    if not event_ids:
        return
    with get_conn() as c:
        ph = ",".join("?" * len(event_ids))
        c.execute(f"DELETE FROM event_queue WHERE event_id IN ({ph})", event_ids)
        c.commit()


def mark_failed(event_ids: list):
    """Sync failed — reset to pending so it retries next cycle."""
    if not event_ids:
        return
    with get_conn() as c:
        ph = ",".join("?" * len(event_ids))
        c.execute(
            f"""UPDATE event_queue
                SET status='pending', retry_count=retry_count+1, last_retry=?
                WHERE event_id IN ({ph})""",
            [_now()] + list(event_ids)
        )
        c.commit()


def requeue_stuck(max_age_seconds: int = 120):
    """
    Safety net: if events were marked 'syncing' but the bot crashed/restarted
    before the ACK, they would be stuck forever. Reset old 'syncing' rows back
    to 'pending' so they retry. Called periodically by the sync loop.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    with get_conn() as c:
        c.execute(
            """UPDATE event_queue SET status='pending'
               WHERE status='syncing' AND created_at < ?""",
            (cutoff,)
        )
        c.commit()


def get_queue_stats():
    with get_conn() as c:
        pending = c.execute("SELECT COUNT(*) FROM event_queue WHERE status='pending'").fetchone()[0]
        syncing = c.execute("SELECT COUNT(*) FROM event_queue WHERE status='syncing'").fetchone()[0]
        oldest = c.execute(
            "SELECT created_at FROM event_queue ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return {"pending": pending, "syncing": syncing, "oldest_event": oldest[0] if oldest else None}


# ─── BAN CACHE ──────────────────────────────────────────────────────────────

def cache_ban(user_id: int, ban_until, permanent, reason: str):
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO ban_cache
               (user_id, ban_until, permanent, reason, cached_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, ban_until, int(bool(permanent)), reason, _now())
        )
        c.commit()


def remove_ban_cache(user_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM ban_cache WHERE user_id=?", (user_id,))
        c.commit()


def is_banned_cached(user_id: int) -> bool:
    with get_conn() as c:
        row = c.execute(
            "SELECT ban_until, permanent FROM ban_cache WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return False
    if row["permanent"]:
        return True
    if row["ban_until"]:
        try:
            bu = datetime.fromisoformat(row["ban_until"]).replace(tzinfo=None)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            return bu > now
        except Exception:
            return False
    return False


def purge_expired_bans():
    """Remove expired temp bans from the cache (auto-unban). Called by cleanup."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with get_conn() as c:
        c.execute(
            "DELETE FROM ban_cache WHERE permanent=0 AND ban_until IS NOT NULL AND ban_until <= ?",
            (now,)
        )
        c.commit()


# ─── USER CACHE ─────────────────────────────────────────────────────────────

_USER_COLS = [
    "user_id", "username", "first_name", "gender", "age", "country",
    "country_flag", "is_premium", "premium_until", "referral_code",
    "referral_count", "messages_sent", "media_approved", "cached_at",
]


def cache_user(user_id: int, data: dict):
    """Insert or replace a full user cache row (used by startup sync)."""
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO user_cache
               (user_id, username, first_name, gender, age, country, country_flag,
                is_premium, premium_until, referral_code, referral_count,
                messages_sent, media_approved, cached_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                data.get("username"),
                data.get("first_name"),
                data.get("gender"),
                data.get("age"),
                data.get("country"),
                data.get("country_flag"),
                int(bool(data.get("is_premium", 0))),
                data.get("premium_until"),
                data.get("referral_code"),
                int(data.get("referral_count") or 0),
                int(data.get("messages_sent") or 0),
                int(data.get("media_approved") or 0),
                _now(),
            )
        )
        c.commit()


def get_cached_user(user_id: int):
    with get_conn() as c:
        row = c.execute("SELECT * FROM user_cache WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def upsert_user_cache_field(user_id: int, **fields):
    """Update specific fields for an existing cached user (creates row if missing)."""
    if not fields:
        return
    with get_conn() as c:
        exists = c.execute("SELECT 1 FROM user_cache WHERE user_id=?", (user_id,)).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO user_cache (user_id, cached_at) VALUES (?, ?)",
                (user_id, _now())
            )
        fields = dict(fields)
        fields["cached_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        c.execute(
            f"UPDATE user_cache SET {cols} WHERE user_id=?",
            (*fields.values(), user_id)
        )
        c.commit()


def increment_messages(user_id: int):
    with get_conn() as c:
        c.execute(
            "UPDATE user_cache SET messages_sent=messages_sent+1, cached_at=? WHERE user_id=?",
            (_now(), user_id)
        )
        c.commit()


def increment_media(user_id: int):
    with get_conn() as c:
        c.execute(
            "UPDATE user_cache SET media_approved=media_approved+1, cached_at=? WHERE user_id=?",
            (_now(), user_id)
        )
        c.commit()


def increment_referral(user_id: int):
    """Local optimistic increment for instant UI. Laptop holds the real count."""
    with get_conn() as c:
        c.execute(
            "UPDATE user_cache SET referral_count=referral_count+1, cached_at=? WHERE user_id=?",
            (_now(), user_id)
        )
        c.commit()


def get_user_by_refcode(code: str):
    if not code:
        return None
    with get_conn() as c:
        row = c.execute("SELECT * FROM user_cache WHERE referral_code=?", (code,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str):
    if not username:
        return None
    uname = username.strip().lstrip("@")
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM user_cache WHERE LOWER(username)=LOWER(?)", (uname,)
        ).fetchone()
    return dict(row) if row else None


def iter_user_ids(batch: int, offset: int):
    """For admin broadcast — fetch a page of user ids from the cache."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT user_id FROM user_cache ORDER BY user_id LIMIT ? OFFSET ?",
            (batch, offset)
        ).fetchall()
    return [r["user_id"] for r in rows]


def local_counts():
    """Lightweight counts from the local cache (for /stats when laptop offline)."""
    with get_conn() as c:
        users = c.execute("SELECT COUNT(*) FROM user_cache").fetchone()[0]
        bans = c.execute("SELECT COUNT(*) FROM ban_cache").fetchone()[0]
        premium = c.execute(
            "SELECT COUNT(*) FROM user_cache WHERE is_premium=1"
        ).fetchone()[0]
    return {"cached_users": users, "cached_bans": bans, "cached_premium": premium}
