"""
sync_engine.py — Render side background sync
--------------------------------------------
Background thread that:
  1. Every SYNC_INTERVAL seconds, fetches a batch of pending events
  2. Sends the batch to the laptop FastAPI master DB
  3. On ACK  -> deletes synced events from the queue
  4. On fail -> resets them to pending (retry next cycle)
  5. Applies "side effects" the laptop sends back (ban / premium changes)
     - updates the local cache
     - fires a notification callback so the bot can message the user

On startup it pulls fresh state (bans + users) from the laptop so the
Render cache is warm even after a free-tier restart.

Laptop offline => events just accumulate in the queue. No crash, no block.
"""

import threading
import time
import uuid
import logging
import json
import os

import requests

from queue_db import (
    init_queue_db, enqueue_event,
    fetch_pending_batch, mark_synced, mark_failed, requeue_stuck,
    get_queue_stats, cache_ban, remove_ban_cache,
    cache_user, upsert_user_cache_field,
)

logger = logging.getLogger(__name__)

# ─── CONFIG ─────────────────────────────────────────────────────────────────
LAPTOP_URL      = os.getenv("LAPTOP_URL", "https://your-tunnel.trycloudflare.com").rstrip("/")
API_SECRET      = os.getenv("API_SECRET", "change-this-secret-key")
SYNC_INTERVAL   = int(os.getenv("SYNC_INTERVAL", "30"))   # seconds between sync attempts
BATCH_SIZE      = int(os.getenv("BATCH_SIZE", "50"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

HEADERS = {"X-API-Secret": API_SECRET, "Content-Type": "application/json"}

_laptop_was_online = None     # tri-state so logs aren't spammy

# Notification callback — bot.py registers a function here.
# It receives a single side-effect dict and sends the right Telegram message.
_notify_callback = None


def set_notify_callback(fn):
    """bot.py calls this so the sync engine can trigger user-facing messages."""
    global _notify_callback
    _notify_callback = fn


def laptop_online() -> bool:
    return _laptop_was_online is True


# ─── STARTUP SYNC ────────────────────────────────────────────────────────────

def startup_sync():
    """Pull all active bans + users from the laptop so the cache is warm."""
    logger.info("[SYNC] Startup sync starting...")
    try:
        resp = requests.get(f"{LAPTOP_URL}/sync/bans", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            bans = resp.json().get("bans", [])
            for b in bans:
                cache_ban(b["user_id"], b.get("ban_until"), b.get("permanent", False), b.get("reason", ""))
            logger.info(f"[SYNC] Startup: cached {len(bans)} bans")

        page, total = 0, 0
        while True:
            resp = requests.get(
                f"{LAPTOP_URL}/sync/users", headers=HEADERS,
                params={"offset": page * 500, "limit": 500}, timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                break
            users = resp.json().get("users", [])
            if not users:
                break
            for u in users:
                cache_user(u["user_id"], u)
            total += len(users)
            page += 1
            if len(users) < 500:
                break
        logger.info(f"[SYNC] Startup: cached {total} users")
        global _laptop_was_online
        _laptop_was_online = True

    except requests.exceptions.ConnectionError:
        logger.warning("[SYNC] Laptop offline at startup — running with empty cache")
    except Exception as e:
        logger.error(f"[SYNC] Startup sync error: {e}")


def get_laptop_stats():
    """Fetch /admin/stats from the laptop (None if offline)."""
    try:
        resp = requests.get(f"{LAPTOP_URL}/admin/stats", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ─── MAIN SYNC LOOP ──────────────────────────────────────────────────────────

def sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL)
        try:
            requeue_stuck(max_age_seconds=max(120, SYNC_INTERVAL * 3))
            _do_sync()
        except Exception as e:
            logger.error(f"[SYNC] Unhandled error in sync loop: {e}")


def _do_sync():
    global _laptop_was_online

    batch = fetch_pending_batch(BATCH_SIZE)
    if not batch:
        return
    event_ids = [e["event_id"] for e in batch]

    payload = {
        "events": [
            {
                "event_id":    e["event_id"],
                "user_id":     e["user_id"],
                "event_type":  e["event_type"],
                "payload":     json.loads(e["payload"]),
                "created_at":  e["created_at"],
                "retry_count": e["retry_count"],
            }
            for e in batch
        ]
    }

    try:
        resp = requests.post(
            f"{LAPTOP_URL}/sync/events", headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 200:
            ack = resp.json()
            processed = ack.get("processed", [])
            failed = ack.get("failed", [])
            effects = ack.get("side_effects", [])

            mark_synced(processed)
            mark_failed(failed)
            _apply_side_effects(effects)

            if _laptop_was_online is not True:
                logger.info("[SYNC] Laptop is online")
            _laptop_was_online = True

            if processed:
                stats = get_queue_stats()
                logger.info(
                    f"[SYNC] Sent {len(processed)} | Failed {len(failed)} | "
                    f"Queue remaining {stats['pending']}"
                )
        else:
            mark_failed(event_ids)
            logger.warning(f"[SYNC] Laptop returned {resp.status_code} — will retry")

    except requests.exceptions.ConnectionError:
        mark_failed(event_ids)
        if _laptop_was_online is not False:
            logger.warning("[SYNC] Laptop offline — events queued, will retry")
        _laptop_was_online = False
    except requests.exceptions.Timeout:
        mark_failed(event_ids)
        logger.warning("[SYNC] Laptop timeout — will retry")
    except Exception as e:
        mark_failed(event_ids)
        logger.error(f"[SYNC] Unexpected sync error: {e}")


def _apply_side_effects(effects: list):
    """
    Laptop tells Render what changed after processing a batch.
    1) update the local cache  2) notify the user via the registered callback.
    """
    for effect in effects:
        etype = effect.get("type")
        try:
            if etype == "BAN_USER":
                cache_ban(
                    effect["user_id"], effect.get("ban_until"),
                    effect.get("permanent", False), effect.get("reason", "auto"),
                )
            elif etype == "UNBAN_USER":
                remove_ban_cache(effect["user_id"])
            elif etype == "PREMIUM_ACTIVATED":
                fields = {"is_premium": 1, "premium_until": effect.get("premium_until")}
                if "referral_count" in effect:
                    fields["referral_count"] = effect["referral_count"]
                upsert_user_cache_field(effect["user_id"], **fields)
            elif etype == "PREMIUM_EXPIRED":
                upsert_user_cache_field(effect["user_id"], is_premium=0, premium_until=None)
            elif etype == "REFERRAL_PROGRESS":
                upsert_user_cache_field(
                    effect["user_id"], referral_count=effect.get("referral_count", 0)
                )

            # fire user-facing notification (best effort)
            if _notify_callback:
                try:
                    _notify_callback(effect)
                except Exception as e:
                    logger.error(f"[SYNC] notify callback error ({etype}): {e}")

        except Exception as e:
            logger.error(f"[SYNC] Side effect apply error ({etype}): {e}")


# ─── PUBLIC HELPERS (used by bot.py) ─────────────────────────────────────────

def push_event(user_id: int, event_type: str, payload: dict) -> str:
    """Queue one event. Returns the event_id (a UUID => idempotent on laptop)."""
    event_id = str(uuid.uuid4())
    enqueue_event(event_id, user_id, event_type, payload)
    return event_id


def start_sync_thread():
    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()
    logger.info(f"[SYNC] Sync thread started (interval={SYNC_INTERVAL}s)")


def init_distributed_system():
    """Call once at bot startup, before polling."""
    init_queue_db()      # 1. local temp queue + cache
    startup_sync()       # 2. warm cache from laptop (ok if offline)
    start_sync_thread()  # 3. background sync
    logger.info("[DIST] Distributed system initialized")
