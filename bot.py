#!/usr/bin/env python3
import os
import re
import random
import threading
import logging
from datetime import datetime, timedelta, timezone
import telebot
from telebot import types
from flask import Flask

# ── Distributed system: Render temp queue/cache + Laptop master DB ──
from queue_db import (
    get_cached_user, cache_user, upsert_user_cache_field,
    is_banned_cached, cache_ban, remove_ban_cache, purge_expired_bans,
    increment_messages, increment_media, increment_referral,
    get_user_by_refcode, get_user_by_username, iter_user_ids, local_counts,
    get_queue_stats,
)
from sync_engine import (
    push_event, init_distributed_system, set_notify_callback,
    get_laptop_stats, laptop_online,
)

# ============================================
# CONFIG
# ============================================

BASEDIR = os.getcwd()

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "YOUR_TOKEN_HERE"
ADMIN_ID = int(os.getenv("ADMIN_ID", 8361006824))

# Content-warning ban: decided INSTANTLY on Render so it works even if laptop is offline.
WARNING_LIMIT = int(os.getenv("WARNING_LIMIT", "2"))
TEMP_BAN_HOURS = int(os.getenv("TEMP_BAN_HOURS", "24"))

# DISPLAY ONLY. The LAPTOP actually decides when premium is granted.
# Keep these EQUAL to REFERRAL_THRESHOLD / PREMIUM_HOURS in laptop_server.py.
PREMIUM_REFERRALS_NEEDED = int(os.getenv("REFERRAL_THRESHOLD", "5"))
PREMIUM_DURATION_HOURS = int(os.getenv("PREMIUM_HOURS", "1"))

# ============================================
# LOGGING
# ============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================
# TELEBOT & FLASK
# ============================================

bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)

# sanitize null bytes from text
_orig_send = bot.send_message
def _send(chat_id, text, *a, **kw):
    t = text.replace("\x00", "") if isinstance(text, str) else text
    return _orig_send(chat_id, t, *a, **kw)
bot.send_message = _send

# ============================================
# RUNTIME STATE
# ============================================

waiting_random = []       # [uid, ...]
waiting_opposite = []     # [(uid, gender), ...]
active_pairs = {}         # {uid: partner_uid}

user_warnings = {}        # {uid: int}
chat_history = {}         # {uid: [(chat_id, msg_id), ...]}
report_reason_pending = {}# {uid: partner_id}  -- chat frozen
pending_country = set()   # uids awaiting country input
games = {}                # {uid: game_dict}  -- same dict for both players

# ============================================
# BANNED CONTENT
# ============================================

BANNED_WORDS = [
    "fuck", "fucking", "sex chat", "nudes", "pussy", "dick", "cock", "penis",
    "vagina", "boobs", "tits", "asshole", "bitch", "slut", "whore", "hoe",
    "prostitute", "porn", "pornography", "rape", "molest", "randi", "maderchod",
    "bsdk", "lauda", "lund", "chut", "choot", "gand", "gaand", "mkc"
]
LINK_PATTERN = re.compile(r"https?|www\.", re.IGNORECASE)
BANNED_PATTERNS = [re.compile(re.escape(w), re.IGNORECASE) for w in BANNED_WORDS]

def is_banned_content(text):
    if not text:
        return False
    if LINK_PATTERN.search(text):
        return True
    return any(p.search(text) for p in BANNED_PATTERNS)

# ============================================
# COUNTRIES
# ============================================

COUNTRY_FLAGS = {
    "afghanistan": "🇦🇫", "albania": "🇦🇱", "algeria": "🇩🇿", "andorra": "🇦🇩",
    "angola": "🇦🇴", "argentina": "🇦🇷", "armenia": "🇦🇲", "australia": "🇦🇺",
    "austria": "🇦🇹", "azerbaijan": "🇦🇿", "bahamas": "🇧🇸", "bahrain": "🇧🇭",
    "bangladesh": "🇧🇩", "barbados": "🇧🇧", "belarus": "🇧🇾", "belgium": "🇧🇪",
    "belize": "🇧🇿", "benin": "🇧🇯", "bhutan": "🇧🇹", "bolivia": "🇧🇴",
    "bosnia and herzegovina": "🇧🇦", "botswana": "🇧🇼", "brazil": "🇧🇷",
    "brunei": "🇧🇳", "bulgaria": "🇧🇬", "burkina faso": "🇧🇫", "burundi": "🇧🇮",
    "cambodia": "🇰🇭", "cameroon": "🇨🇲", "canada": "🇨🇦", "cape verde": "🇨🇻",
    "central african republic": "🇨🇫", "chad": "🇹🇩", "chile": "🇨🇱", "china": "🇨🇳",
    "colombia": "🇨🇴", "comoros": "🇰🇲", "congo": "🇨🇬", "costa rica": "🇨🇷",
    "croatia": "🇭🇷", "cuba": "🇨🇺", "cyprus": "🇨🇾", "czech republic": "🇨🇿",
    "denmark": "🇩🇰", "djibouti": "🇩🇯", "dominica": "🇩🇲",
    "dominican republic": "🇩🇴", "ecuador": "🇪🇨", "egypt": "🇪🇬",
    "el salvador": "🇸🇻", "equatorial guinea": "🇬🇶", "eritrea": "🇪🇷",
    "estonia": "🇪🇪", "eswatini": "🇸🇿", "ethiopia": "🇪🇹", "fiji": "🇫🇯",
    "finland": "🇫🇮", "france": "🇫🇷", "gabon": "🇬🇦", "gambia": "🇬🇲",
    "georgia": "🇬🇪", "germany": "🇩🇪", "ghana": "🇬🇭", "greece": "🇬🇷",
    "grenada": "🇬🇩", "guatemala": "🇬🇹", "guinea": "🇬🇳", "guinea-bissau": "🇬🇼",
    "guyana": "🇬🇾", "haiti": "🇭🇹", "honduras": "🇭🇳", "hungary": "🇭🇺",
    "iceland": "🇮🇸", "india": "🇮🇳", "indonesia": "🇮🇩", "iran": "🇮🇷",
    "iraq": "🇮🇶", "ireland": "🇮🇪", "israel": "🇮🇱", "italy": "🇮🇹",
    "jamaica": "🇯🇲", "japan": "🇯🇵", "jordan": "🇯🇴", "kazakhstan": "🇰🇿",
    "kenya": "🇰🇪", "kiribati": "🇰🇮", "korea north": "🇰🇵", "korea south": "🇰🇷",
    "kuwait": "🇰🇼", "kyrgyzstan": "🇰🇬", "laos": "🇱🇦", "latvia": "🇱🇻",
    "lebanon": "🇱🇧", "lesotho": "🇱🇸", "liberia": "🇱🇷", "libya": "🇱🇾",
    "liechtenstein": "🇱🇮", "lithuania": "🇱🇹", "luxembourg": "🇱🇺",
    "madagascar": "🇲🇬", "malawi": "🇲🇼", "malaysia": "🇲🇾", "maldives": "🇲🇻",
    "mali": "🇲🇱", "malta": "🇲🇹", "marshall islands": "🇲🇭", "mauritania": "🇲🇷",
    "mauritius": "🇲🇺", "mexico": "🇲🇽", "micronesia": "🇫🇲", "moldova": "🇲🇩",
    "monaco": "🇲🇨", "mongolia": "🇲🇳", "montenegro": "🇲🇪", "morocco": "🇲🇦",
    "mozambique": "🇲🇿", "myanmar": "🇲🇲", "namibia": "🇳🇦", "nauru": "🇳🇷",
    "nepal": "🇳🇵", "netherlands": "🇳🇱", "new zealand": "🇳🇿", "nicaragua": "🇳🇮",
    "niger": "🇳🇪", "nigeria": "🇳🇬", "north macedonia": "🇲🇰", "norway": "🇳🇴",
    "oman": "🇴🇲", "pakistan": "🇵🇰", "palau": "🇵🇼", "palestine": "🇵🇸",
    "panama": "🇵🇦", "papua new guinea": "🇵🇬", "paraguay": "🇵🇾", "peru": "🇵🇪",
    "philippines": "🇵🇭", "poland": "🇵🇱", "portugal": "🇵🇹", "qatar": "🇶🇦",
    "romania": "🇷🇴", "russia": "🇷🇺", "rwanda": "🇷🇼",
    "saint kitts and nevis": "🇰🇳", "saint lucia": "🇱🇨",
    "saint vincent and the grenadines": "🇻🇨", "samoa": "🇼🇸", "san marino": "🇸🇲",
    "sao tome and principe": "🇸🇹", "saudi arabia": "🇸🇦", "senegal": "🇸🇳",
    "serbia": "🇷🇸", "seychelles": "🇸🇨", "sierra leone": "🇸🇱", "singapore": "🇸🇬",
    "slovakia": "🇸🇰", "slovenia": "🇸🇮", "solomon islands": "🇸🇧", "somalia": "🇸🇴",
    "south africa": "🇿🇦", "south sudan": "🇸🇸", "spain": "🇪🇸", "sri lanka": "🇱🇰",
    "sudan": "🇸🇩", "suriname": "🇸🇷", "sweden": "🇸🇪", "switzerland": "🇨🇭",
    "syria": "🇸🇾", "taiwan": "🇹🇼", "tajikistan": "🇹🇯", "tanzania": "🇹🇿",
    "thailand": "🇹🇭", "timor-leste": "🇹🇱", "togo": "🇹🇬", "tonga": "🇹🇴",
    "trinidad and tobago": "🇹🇹", "tunisia": "🇹🇳", "turkey": "🇹🇷",
    "turkmenistan": "🇹🇲", "tuvalu": "🇹🇻", "uganda": "🇺🇬", "ukraine": "🇺🇦",
    "united arab emirates": "🇦🇪", "united kingdom": "🇬🇧", "united states": "🇺🇸",
    "uruguay": "🇺🇾", "uzbekistan": "🇺🇿", "vanuatu": "🇻🇺", "vatican city": "🇻🇦",
    "venezuela": "🇻🇪", "vietnam": "🇻🇳", "yemen": "🇾🇪", "zambia": "🇿🇲",
    "zimbabwe": "🇿🇼",
}

COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "america": "united states",
    "uk": "united kingdom", "britain": "united kingdom",
    "uae": "united arab emirates", "south korea": "korea south",
    "north korea": "korea north", "czechia": "czech republic",
}

def get_country_info(user_input):
    if not user_input:
        return None
    n = user_input.strip().lower()
    n = COUNTRY_ALIASES.get(n, n)
    if n in COUNTRY_FLAGS:
        return (n.title(), COUNTRY_FLAGS[n])
    return None

# ============================================
# DATABASE
# ============================================

def _gen_ref_code(uid):
    return f"REF{uid}{random.randint(1000, 99999)}"


def db_get_user(uid):
    """Read from local cache (warmed from laptop on startup / via side-effects)."""
    return get_cached_user(uid)


def db_create_user(tg_user):
    existing = get_cached_user(tg_user.id)
    if existing:
        # keep username / first_name fresh
        if (existing.get("username") or "") != (tg_user.username or "") or \
           (existing.get("first_name") or "") != (tg_user.first_name or ""):
            upsert_user_cache_field(
                tg_user.id,
                username=tg_user.username or "",
                first_name=tg_user.first_name or "",
            )
            push_event(tg_user.id, "PROFILE_UPDATE", {
                "username": tg_user.username or "",
                "first_name": tg_user.first_name or "",
            })
        return
    code = _gen_ref_code(tg_user.id)
    cache_user(tg_user.id, {
        "username": tg_user.username or "",
        "first_name": tg_user.first_name or "",
        "gender": None, "age": None, "country": None, "country_flag": None,
        "is_premium": 0, "premium_until": None,
        "referral_code": code, "referral_count": 0,
        "messages_sent": 0, "media_approved": 0,
    })
    push_event(tg_user.id, "REGISTER", {
        "username": tg_user.username or "",
        "first_name": tg_user.first_name or "",
        "referral_code": code,
        "joined_at": datetime.now(timezone.utc).isoformat(),
    })


def db_update(uid, **fields):
    """Profile updates: cache instantly + push PROFILE_UPDATE.
    Premium changes must go through db_set_premium/db_remove_premium."""
    if not fields:
        return
    if "premium_until" in fields and fields["premium_until"] is None and len(fields) == 1:
        db_remove_premium(uid)
        return
    upsert_user_cache_field(uid, **fields)
    profile = {k: v for k, v in fields.items()
               if k in ("gender", "age", "country", "country_flag", "username", "first_name")}
    if profile:
        push_event(uid, "PROFILE_UPDATE", profile)


def db_is_premium(uid):
    if uid == ADMIN_ID:
        return True
    u = get_cached_user(uid)
    if not u or not u.get("premium_until"):
        return False
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pu = datetime.fromisoformat(u["premium_until"])
        if pu.tzinfo is not None:
            pu = pu.replace(tzinfo=None)
        return pu > now
    except Exception:
        return False


def db_set_premium(uid, until_str):
    """Admin: add premium until a date/datetime. Symmetric with db_remove_premium."""
    try:
        s = f"{until_str}T23:59:59" if len(until_str) == 10 else until_str
        dt = datetime.fromisoformat(s)
        iso = dt.isoformat()
        upsert_user_cache_field(uid, is_premium=1, premium_until=iso)
        push_event(uid, "PREMIUM_SET", {"premium_until": iso})
        return True
    except Exception:
        return False


def db_remove_premium(uid):
    """Admin: remove premium. Mirror of db_set_premium."""
    upsert_user_cache_field(uid, is_premium=0, premium_until=None)
    push_event(uid, "PREMIUM_SET", {"premium_until": None})


def db_is_banned(uid):
    if uid == ADMIN_ID:
        return False
    return is_banned_cached(uid)


def db_ban(uid, hours=None, permanent=False, reason=""):
    """Admin / content ban: decided here, cached instantly, pushed to master.
    Symmetric with db_unban."""
    ban_until = None if permanent else (
        datetime.now(timezone.utc) + timedelta(hours=hours or TEMP_BAN_HOURS)
    ).isoformat()
    cache_ban(uid, ban_until, permanent, reason)
    push_event(uid, "BAN_USER", {
        "hours": hours or TEMP_BAN_HOURS,
        "permanent": bool(permanent),
        "reason": reason,
    })


def db_unban(uid):
    """Mirror of db_ban. Also clears the user's report counter on the master."""
    remove_ban_cache(uid)
    push_event(uid, "UNBAN_USER", {})


def db_add_report(reporter_id, reported_id, report_type, reason):
    """Laptop counts reports and decides the auto-ban (returns a BAN_USER side-effect)."""
    push_event(reporter_id, "REPORT_USER", {
        "reported_id": reported_id,
        "report_type": report_type,
        "reason": reason,
    })


def db_add_referral(referrer_id):
    """Laptop counts referrals and grants premium. Local bump = instant UI only."""
    increment_referral(referrer_id)
    push_event(referrer_id, "REFERRAL_INCREMENT", {"referrer_id": referrer_id})


def db_count_message(uid):
    increment_messages(uid)
    push_event(uid, "MESSAGE", {})


def db_count_media(uid):
    increment_media(uid)
    push_event(uid, "MEDIA", {})


# cached at startup — avoid calling get_me() on every refer link request
_bot_username = None


def get_bot_username():
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = bot.get_me().username
        except Exception:
            pass
    return _bot_username


def db_get_referral_link(uid):
    u = db_get_user(uid)
    if not u:
        return None
    uname = get_bot_username()
    if uname:
        return f"https://t.me/{uname}?start={u['referral_code']}"
    return f"REFCODE:{u['referral_code']}"


def resolve_user(identifier):
    if not identifier:
        return None
    try:
        return int(identifier.strip())
    except Exception:
        pass
    u = get_user_by_username(identifier)
    return u["user_id"] if u else None


def user_label(uid):
    u = db_get_user(uid)
    return f"@{u['username']}" if (u and u.get("username")) else str(uid)


# ============================================
# KEYBOARDS
# ============================================

def main_kb(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🔀 Search Random")
    u = db_get_user(uid)
    if u and u["gender"]:
        kb.add("♀️ Search Opposite Gender" if db_is_premium(uid) else "♀️ Opposite Gender (Premium)")
    kb.add("⚙️ Settings", "🔗 Refer")
    kb.add("📖 Help")
    return kb

def chat_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📊 Stats", "🚩 Report")
    kb.add("⏭️ Next", "🛑 Stop")
    return kb

def report_kb():
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton("🔀 Spam", callback_data="rep:spam"),
        types.InlineKeyboardButton("🚫 Unwanted Content", callback_data="rep:unwanted"),
        types.InlineKeyboardButton("😠 Inappropriate Messages", callback_data="rep:inappropriate"),
        types.InlineKeyboardButton("🤔 Suspicious Activity", callback_data="rep:suspicious"),
        types.InlineKeyboardButton("❓ Other", callback_data="rep:other"),
    )
    return m

# ============================================
# PARTNER MSG
# ============================================

def partner_msg(partner_data, viewer_id):
    g = "♂️" if partner_data["gender"] == "Male" else "♀️" if partner_data["gender"] == "Female" else "❓"
    age = str(partner_data["age"]) if partner_data["age"] else "Unknown"
    flag = partner_data["country_flag"] or "🌍"
    country = partner_data["country"] or "Global"
    msg = f"✅ Partner Found!\n\n🎂 Age: {age}\n{g} Gender: {partner_data['gender'] or 'Unknown'}\n{flag} Country: {country}\n\n"
    if viewer_id == ADMIN_ID:
        msg += f"👤 Name: {partner_data['first_name'] or partner_data['username'] or 'Unknown'}\n🆔 ID: {partner_data['user_id']}\n\n"
    msg += "💬 Start chatting!\n🎮 /game to play • 🛑 Stop • ⏭️ Next"
    return msg

# ============================================
# QUEUE & MATCHING
# ============================================

def remove_from_queues(uid):
    global waiting_random, waiting_opposite
    if uid in waiting_random:
        waiting_random.remove(uid)
    waiting_opposite[:] = [(u, g) for u, g in waiting_opposite if u != uid]

def match_users():
    global waiting_random, waiting_opposite, active_pairs

    # opposite gender first
    i = 0
    while i < len(waiting_opposite):
        uid, s_gender = waiting_opposite[i]
        want = "Female" if s_gender == "Male" else "Male"
        found_j = next(
            (j for j, other in enumerate(waiting_random)
             if (db_get_user(other) or {}).get("gender") == want),
            None
        )
        if found_j is not None:
            other = waiting_random.pop(found_j)
            waiting_opposite.pop(i)
            _connect(uid, other)
            return
        i += 1

    # random
    while len(waiting_random) >= 2:
        u1 = waiting_random.pop(0)
        u2 = waiting_random.pop(0)
        _connect(u1, u2)

def _connect(u1, u2):
    active_pairs[u1] = u2
    active_pairs[u2] = u1
    d1, d2 = db_get_user(u1), db_get_user(u2)
    try:
        bot.send_message(u1, partner_msg(d2, u1), reply_markup=chat_kb())
    except:
        pass
    try:
        bot.send_message(u2, partner_msg(d1, u2), reply_markup=chat_kb())
    except:
        pass
    logger.info(f"Matched: {u1} <-> {u2}")

def disconnect_user(uid, notify_partner=True, reason="left"):
    partner = active_pairs.pop(uid, None)
    if partner:
        active_pairs.pop(partner, None)
        # games + pending media cleanup
        games.pop(uid, None)
        games.pop(partner, None)
        report_reason_pending.pop(uid, None)
        report_reason_pending.pop(partner, None)
        if notify_partner:
            try:
                # "left" = normal disconnect, "banned" = admin kicked
                if reason == "banned":
                    title = "🚫 Partner was removed"
                else:
                    title = "Partner ended chat 🚫"

                # Single message: title + /search hint, with only Report button
                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    types.InlineKeyboardButton("🚩 Report", callback_data=f"exrep:{uid}")
                )
                bot.send_message(
                    partner,
                    f"{title}\n/search to find new partner",
                    reply_markup=markup
                )
            except:
                pass
    remove_from_queues(uid)

# ============================================
# GAME: GUESS THE NUMBER
# ============================================

def start_guess_game(initiator, partner):
    secret = random.randint(1, 10)
    # use a list so both share same mutable state
    state = {
        "type": "guess",
        "secret": secret,
        "guesser": partner,
        "initiator": initiator,
        "attempts": 0,
    }
    games[initiator] = state
    games[partner] = state
    try:
        bot.send_message(initiator,
            "🔢 Guess the Number started!\n\n"
            f"Secret number set (1-10). Your partner will try to guess it.\n"
            "You'll see their attempts. Good luck!\n\n"
            "Use /endgame to quit.")
        bot.send_message(partner,
            "🔢 Guess the Number!\n\n"
            "Your partner picked a number 1-10.\n"
            "Just type a number to guess!\n\n"
            "Use /endgame to quit.")
    except:
        pass

def handle_guess_input(uid, text):
    """Returns True if input was consumed by game"""
    state = games.get(uid)
    if not state or state["type"] != "guess":
        return False
    # only guesser's number inputs go here
    if uid != state["guesser"]:
        return False  # initiator's messages should pass through as chat
    if not text.strip().lstrip("-").isdigit():
        return False  # not a number, let it flow as chat

    guess = int(text.strip())
    if guess < 1 or guess > 10:
        bot.send_message(uid, "🎮 Guess must be between 1 and 10!")
        return True

    state["attempts"] += 1
    secret = state["secret"]
    initiator = state["initiator"]
    attempts = state["attempts"]

    if guess == secret:
        bot.send_message(uid,
            f"🎉 Correct! The number was {secret}!\n"
            f"You got it in {attempts} {'attempt' if attempts == 1 else 'attempts'}! 🏆")
        bot.send_message(initiator,
            f"😅 Your partner guessed {secret} in {attempts} {'attempt' if attempts == 1 else 'attempts'}!\n"
            "They win! Start a new game with /game")
        games.pop(uid, None)
        games.pop(initiator, None)
    elif guess < secret:
        bot.send_message(uid, f"⬆️ Too low! Go higher. (Attempt {attempts})")
    else:
        bot.send_message(uid, f"⬇️ Too high! Go lower. (Attempt {attempts})")
    return True

# ============================================
# GAME: WORD CHAIN
# ============================================

def start_word_chain(initiator, partner):
    state = {
        "type": "word",
        "turn": initiator,
        "initiator": initiator,
        "other": partner,
        "last_letter": None,
        "used_words": [],
    }
    games[initiator] = state
    games[partner] = state
    try:
        bot.send_message(initiator,
            "📝 Word Chain started!\n\n"
            "You go first! Send a word with:\n/word <yourword>\n\n"
            "Rules:\n"
            "• Each word must start with the LAST letter of the previous word\n"
            "• No repeating words\n"
            "• Letters only, no spaces\n\n"
            "Use /endgame to quit.")
        bot.send_message(partner,
            "📝 Word Chain started!\n\n"
            "Waiting for partner's first word...\n"
            "When it's your turn, type:\n/word <yourword>\n\n"
            "Use /endgame to quit.")
    except:
        pass

def handle_word_move(uid, word):
    """Process a /word move. Returns True if move was handled."""
    state = games.get(uid)
    if not state or state["type"] != "word":
        bot.send_message(uid, "❌ Not in a Word Chain game. Use /game to start one.")
        return True

    other = state["other"] if uid == state["initiator"] else state["initiator"]

    if state["turn"] != uid:
        bot.send_message(uid, "⏳ Not your turn! Wait for your partner.")
        return True

    w = word.strip().lower()

    if not w.isalpha():
        bot.send_message(uid, "❌ Only letters allowed! No spaces or numbers.\n/word <word>")
        return True

    if w in state["used_words"]:
        bot.send_message(uid, f"❌ '{w}' already used! Try a different word.\n/word <word>")
        return True

    if state["last_letter"] and w[0] != state["last_letter"]:
        bot.send_message(uid,
            f"❌ Word must start with '{state['last_letter'].upper()}'!\n"
            f"/word <word starting with {state['last_letter'].upper()}>")
        return True

    # valid
    state["used_words"].append(w)
    state["last_letter"] = w[-1]
    state["turn"] = other
    next_letter = w[-1].upper()
    count = len(state["used_words"])

    bot.send_message(uid,
        f"✅ '{w.title()}' accepted! ({count} words played)\n"
        f"Partner's turn now. Next letter: {next_letter}")
    bot.send_message(other,
        f"📝 Partner played: '{w.title()}'\n"
        f"Your turn! Word must start with '{next_letter}':\n"
        f"/word <word>")
    return True

# ============================================
# ADMIN FORWARD
# ============================================

def forward_to_admin(reporter_id, reported_id, report_type, reason=""):
    try:
        bot.send_message(ADMIN_ID,
            f"🚩 NEW REPORT\n"
            f"Type: {report_type}\n"
            f"Reason: {reason or '-'}\n"
            f"Reporter: {user_label(reporter_id)} ({reporter_id})\n"
            f"Reported: {user_label(reported_id)} ({reported_id})\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        for label, uid in [("Reporter", reporter_id), ("Reported", reported_id)]:
            msgs = chat_history.get(uid, [])[-10:]
            if msgs:
                bot.send_message(ADMIN_ID, f"📨 {label}'s recent messages:")
                for cid, mid in msgs:
                    try:
                        bot.forward_message(ADMIN_ID, cid, mid)
                    except:
                        pass
        bot.send_message(ADMIN_ID, "━━━━ End of Report ━━━━")
    except Exception as e:
        logger.error(f"Admin forward error: {e}")

# ============================================
# HELPERS
# ============================================

def append_history(uid, chat_id, msg_id, max_len=50):
    h = chat_history.setdefault(uid, [])
    h.append((chat_id, msg_id))
    if len(h) > max_len:
        h.pop(0)

def warn_user(uid, reason):
    count = user_warnings.get(uid, 0) + 1
    user_warnings[uid] = count
    if count >= WARNING_LIMIT:
        db_ban(uid, hours=TEMP_BAN_HOURS, reason=reason)
        user_warnings[uid] = 0
        try:
            bot.send_message(uid, f"⛔ Banned for {TEMP_BAN_HOURS}h.\nReason: {reason}")
        except:
            pass
        remove_from_queues(uid)
        disconnect_user(uid, notify_partner=True, reason="banned")
    else:
        try:
            bot.send_message(uid,
                f"⚠️ Warning {count}/{WARNING_LIMIT}: {reason}\n"
                f"{WARNING_LIMIT - count} more warning(s) = ban.")
        except:
            pass

def profile_complete(uid):
    u = db_get_user(uid)
    return bool(u and u["gender"] and u["age"] and u["country"])

# ============================================
# LAPTOP SIDE-EFFECTS  (master DB -> user notifications)
# ============================================
#
# sync_engine calls this for every side-effect the laptop returns.
# The cache is ALREADY updated by sync_engine; here we just message the user
# and react (e.g. disconnect a freshly-banned user). Wrapped in try/except
# because it runs inside the background sync thread.

def on_side_effect(effect: dict):
    etype = effect.get("type")
    uid = effect.get("user_id")

    if etype == "BAN_USER":
        # auto-ban from reports (laptop decided). Kick + notify.
        try:
            remove_from_queues(uid)
            disconnect_user(uid, notify_partner=True, reason="banned")
        except Exception:
            pass
        reason = effect.get("reason", "Multiple reports")
        try:
            bot.send_message(uid, f"🚫 You have been banned.\nReason: {reason}")
        except Exception:
            pass
        # thank the reporters (auto-ban only)
        for rid in effect.get("reporters", []) or []:
            try:
                bot.send_message(rid,
                    "✅ Action Taken!\nA user you reported has been banned.\n"
                    "Thanks for keeping our community clean! 🧹")
            except Exception:
                pass

    elif etype == "UNBAN_USER":
        try:
            bot.send_message(uid, "✅ Your ban has been lifted! Welcome back.",
                             reply_markup=main_kb(uid))
        except Exception:
            pass

    elif etype == "PREMIUM_ACTIVATED":
        via = effect.get("via")
        if via == "referral":
            txt = (f"🎉 PREMIUM UNLOCKED!\n{PREMIUM_DURATION_HOURS}h premium earned via referrals!\n"
                   "♀️ Opposite gender search unlocked!")
        else:
            txt = "🎉 PREMIUM ACTIVATED!\n♀️ Opposite gender search unlocked!"
        try:
            bot.send_message(uid, txt, reply_markup=main_kb(uid))
        except Exception:
            pass

    elif etype == "PREMIUM_EXPIRED":
        try:
            bot.send_message(uid, "⚠️ Your premium has ended.", reply_markup=main_kb(uid))
        except Exception:
            pass

# ============================================
# PROFILE SETUP STEPS
# ============================================

def process_age_input(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text.isdigit() or not (12 <= int(text) <= 99):
        bot.send_message(uid, "❌ Age must be 12-99. Try again:")
        bot.register_next_step_handler(message, process_age_input)
        return
    db_update(uid, age=int(text))
    u = db_get_user(uid)
    if not u or not u["country"]:
        bot.send_message(uid, f"✅ Age set to {text}!\n\n🌍 Enter your country (e.g. India):")
        pending_country.add(uid)
        bot.register_next_step_handler(message, process_country_input)
    else:
        bot.send_message(uid, f"✅ Age updated to {text}!", reply_markup=main_kb(uid))

def process_country_input(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if uid not in pending_country:
        return
    info = get_country_info(text)
    if not info:
        bot.send_message(uid, f"❌ '{text}' not recognized. Try again (e.g. India, Japan):")
        bot.register_next_step_handler(message, process_country_input)
        return
    name, flag = info
    db_update(uid, country=name, country_flag=flag)
    pending_country.discard(uid)
    bot.send_message(uid,
        f"✅ Country set to {flag} {name}!\n\nProfile complete! Ready to chat? 🎉",
        reply_markup=main_kb(uid))

# ============================================
# FLASK ROUTES
# ============================================

@app.route("/", methods=["GET"])
def home():
    return "FenLiX Bot Running!", 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}, 200

# ============================================================
# RENDER + UPTIME ROBOT SETUP  (commented out by default)
# ============================================================
#
# STEP 1 — Render pe deploy karo:
#   - New Web Service → Connect your GitHub repo
#   - Build Command:  pip install pyTelegramBotAPI flask
#   - Start Command:  python bot.py
#   - Environment Variables set karo:
#       BOT_TOKEN   = <your telegram token>
#       ADMIN_ID    = <your telegram user id>
#       PORT        = 10000   (Render free tier port)
#   - Plan: Free
#
# STEP 2 — UptimeRobot se ping lagao (free):
#   - https://uptimerobot.com  pe account banao
#   - "Add New Monitor" → HTTP(s)
#   - URL: https://<your-render-app-name>.onrender.com/ping
#   - Interval: 5 minutes
#   - Yahi ping Render ko alive rakhegi (free tier 15 min sleep hoti hai)
#
# STEP 3 — Ye route already uncommented hai, kuch karne ki zaroorat nahi:

@app.route("/ping", methods=["GET"])
def ping():
    return "pong", 200
#
# Bas itna hi! Baaki sab already configure hai.
# Jab local run karna ho — comment out rakho, koi farak nahi padta.
# ============================================================

# ============================================
# /start
# ============================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    db_create_user(message.from_user)
    if db_is_banned(uid):
        bot.send_message(uid, "🚫 You are banned from this bot.")
        return

    # referral check
    parts = message.text.split()
    if len(parts) > 1:
        ref_code = parts[1]
        referrer = get_user_by_refcode(ref_code)
        if referrer and referrer["user_id"] != uid:
            db_add_referral(referrer["user_id"])
            bot.send_message(uid, "✅ You joined via a referral link!")

    u = db_get_user(uid)

    if not u or not u["gender"]:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("♂️ Male", callback_data="sex:male"),
            types.InlineKeyboardButton("♀️ Female", callback_data="sex:female")
        )
        bot.send_message(uid,
            "🌐 Welcome to FenLiX!\n\nAnonymous chat platform.\nSelect your gender to get started:",
            reply_markup=markup)
    elif not u["age"]:
        bot.send_message(uid, "📅 Enter your age (12-99):")
        bot.register_next_step_handler(message, process_age_input)
    elif not u["country"]:
        bot.send_message(uid, "🌍 Enter your country (e.g. India):")
        pending_country.add(uid)
        bot.register_next_step_handler(message, process_country_input)
    else:
        ps = "Premium Active ⭐" if db_is_premium(uid) else "Free User"
        g = "♂️" if u["gender"] == "Male" else "♀️"
        bot.send_message(uid,
            f"👋 Welcome back!\n\n{g} {u['gender']} | 📅 {u['age']} | {u['country_flag']} {u['country']}\n\n🎁 {ps}",
            reply_markup=main_kb(uid))

# ============================================
# GENDER CALLBACK (no admin notification)
# ============================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("sex:"))
def cb_gender(call):
    uid = call.from_user.id
    db_create_user(call.from_user)
    if db_is_banned(uid):
        bot.answer_callback_query(call.id, "You are banned.", show_alert=True)
        return
    # FIX: no admin notification at all
    gender = "Male" if call.data == "sex:male" else "Female"
    db_update(uid, gender=gender)
    bot.answer_callback_query(call.id, f"✅ Gender set to {gender}!", show_alert=True)
    try:
        bot.edit_message_text(f"✅ Gender: {gender}", call.message.chat.id, call.message.message_id)
    except:
        pass
    u = db_get_user(uid)
    if not u or not u["age"]:
        bot.send_message(uid, "📅 Enter your age (12-99):")
        bot.register_next_step_handler(call.message, process_age_input)
    elif not u["country"]:
        pending_country.add(uid)
        bot.send_message(uid, "🌍 Enter your country (e.g. India):")
        bot.register_next_step_handler(call.message, process_country_input)
    else:
        bot.send_message(uid, f"✅ Gender updated to {gender}!", reply_markup=main_kb(uid))

# ============================================
# SETTINGS
# ============================================

@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    if not u:
        bot.send_message(uid, "Use /start first.")
        return
    ps = "Premium Active ⭐" if db_is_premium(uid) else "Free User"
    g = "♂️" if u["gender"] == "Male" else "♀️" if u["gender"] == "Female" else "❓"
    text = (
        f"⚙️ YOUR PROFILE\n\n"
        f"{g} Gender: {u['gender'] or 'Not set'}\n"
        f"📅 Age: {u['age'] or 'Not set'}\n"
        f"🌍 Country: {(u['country_flag'] or '') + ' ' + (u['country'] or 'Not set')}\n\n"
        f"📊 STATS\n"
        f"💬 Messages: {u['messages_sent']}\n"
        f"👥 Referred: {u['referral_count']}/{PREMIUM_REFERRALS_NEEDED}\n\n"
        f"🎁 {ps}"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("♂️ Male", callback_data="sex:male"),
        types.InlineKeyboardButton("♀️ Female", callback_data="sex:female")
    )
    markup.row(types.InlineKeyboardButton("📅 Change Age", callback_data="age:change"))
    markup.row(types.InlineKeyboardButton("🌍 Change Country", callback_data="country:change"))
    markup.row(types.InlineKeyboardButton("🔗 My Referral Link", callback_data="ref:link"))
    bot.send_message(uid, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "age:change")
def cb_age(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(uid, "📅 Enter new age (12-99):")
    bot.register_next_step_handler(call.message, process_age_input)

@bot.callback_query_handler(func=lambda c: c.data == "country:change")
def cb_country(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    pending_country.add(uid)
    bot.send_message(uid, "🌍 Enter your new country (e.g. India):")
    bot.register_next_step_handler(call.message, process_country_input)

@bot.callback_query_handler(func=lambda c: c.data == "ref:link")
def cb_ref_link(call):
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    u = db_get_user(uid)
    if not u:
        return
    link = db_get_referral_link(uid)
    remaining = max(0, PREMIUM_REFERRALS_NEEDED - u["referral_count"])
    bot.send_message(uid,
        f"🔗 Your Referral Link:\n{link}\n\n"
        f"👥 Referred: {u['referral_count']}/{PREMIUM_REFERRALS_NEEDED}\n"
        f"{'📢 ' + str(remaining) + ' more friends needed!' if remaining > 0 else '🎉 Goal reached!'}")

# ============================================
# REFER
# ============================================

@bot.message_handler(commands=["refer"])
def cmd_refer(message):
    uid = message.from_user.id
    u = db_get_user(uid)
    if not u:
        bot.send_message(uid, "Use /start first.")
        return
    link = db_get_referral_link(uid)
    remaining = max(0, PREMIUM_REFERRALS_NEEDED - u["referral_count"])
    bot.send_message(uid,
        f"🎁 REFERRAL SYSTEM\n\n"
        f"🔗 Your Link:\n{link}\n\n"
        f"👥 Referred: {u['referral_count']}/{PREMIUM_REFERRALS_NEEDED}\n"
        f"🏆 Reward: {PREMIUM_DURATION_HOURS}h Premium\n\n"
        f"{'📢 Invite ' + str(remaining) + ' more friends!' if remaining > 0 else '🎉 Premium active!'}\n\n"
        f"How it works:\n1️⃣ Share link\n2️⃣ Friend joins\n3️⃣ Get premium after {PREMIUM_REFERRALS_NEEDED} joins!")

# ============================================
# SEARCH
# ============================================

@bot.message_handler(commands=["search_random", "search"])
def cmd_search_random(message):
    uid = message.from_user.id
    if db_is_banned(uid):
        bot.send_message(uid, "🚫 You are banned.")
        return
    if not profile_complete(uid):
        bot.send_message(uid, "⚠️ Complete your profile first! Use /start")
        return
    if uid in active_pairs:
        bot.send_message(uid, "Already in chat! Use ⏭️ Next or 🛑 Stop.")
        return
    in_q = uid in waiting_random or any(u == uid for u, _ in waiting_opposite)
    if in_q:
        bot.send_message(uid, "🔍 Already searching... please wait!")
        return
    remove_from_queues(uid)
    waiting_random.append(uid)
    bot.send_message(uid, "🔍 Searching for a random partner...")
    match_users()

@bot.message_handler(commands=["search_opposite"])
def cmd_search_opposite(message):
    uid = message.from_user.id
    if db_is_banned(uid):
        bot.send_message(uid, "🚫 You are banned.")
        return
    if not db_is_premium(uid):
        bot.send_message(uid,
            f"💎 PREMIUM REQUIRED!\n\nInvite {PREMIUM_REFERRALS_NEEDED} friends to unlock!\n/refer to get your link.")
        return
    if not profile_complete(uid):
        bot.send_message(uid, "⚠️ Complete your profile first! Use /start")
        return
    if uid in active_pairs:
        bot.send_message(uid, "Already in chat! Use ⏭️ Next or 🛑 Stop.")
        return
    u = db_get_user(uid)
    in_q = uid in waiting_random or any(uid == w for w, _ in waiting_opposite)
    if in_q:
        bot.send_message(uid, "🔍 Already searching... please wait!")
        return
    remove_from_queues(uid)
    waiting_opposite.append((uid, u["gender"]))
    bot.send_message(uid, "🔍 Searching for opposite gender partner...")
    match_users()

@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    uid = message.from_user.id
    remove_from_queues(uid)
    disconnect_user(uid, notify_partner=True)
    bot.send_message(uid, "🛑 Stopped. Use the menu to search again.", reply_markup=main_kb(uid))

@bot.message_handler(commands=["next"])
def cmd_next(message):
    uid = message.from_user.id
    in_anything = uid in active_pairs or uid in waiting_random or any(u == uid for u, _ in waiting_opposite)
    if not in_anything:
        bot.send_message(uid, "Not in chat/queue. Use 🔀 Search Random.")
        return
    disconnect_user(uid, notify_partner=True)
    remove_from_queues(uid)
    waiting_random.append(uid)
    bot.send_message(uid, "⏳ Finding next partner...")
    match_users()

# ============================================
# REPORT
# ============================================

@bot.message_handler(commands=["report"])
def cmd_report(message):
    uid = message.from_user.id
    if uid not in active_pairs:
        bot.send_message(uid, "⚠️ You must be in a chat to report someone.")
        return
    bot.send_message(uid, "🚩 Why are you reporting?", reply_markup=report_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("rep:"))
def cb_report(call):
    uid = call.from_user.id
    if uid not in active_pairs:
        bot.answer_callback_query(call.id, "You're not in an active chat.", show_alert=True)
        return
    partner_id = active_pairs[uid]
    rtype = call.data.split(":")[1]
    type_map = {
        "spam": "Spam", "unwanted": "Unwanted Content",
        "inappropriate": "Inappropriate Messages",
        "suspicious": "Suspicious Activity", "other": "Other",
    }
    rtype_name = type_map.get(rtype, "Other")

    if rtype == "other":
        report_reason_pending[uid] = partner_id
        bot.answer_callback_query(call.id)
        bot.send_message(uid,
            "❓ Type a short reason for reporting.\nType 'cancel' to cancel.")
        return

    db_add_report(uid, partner_id, rtype_name, "")
    forward_to_admin(uid, partner_id, rtype_name)
    bot.answer_callback_query(call.id, "Report submitted!", show_alert=False)
    bot.send_message(uid, "✅ Report submitted! Admins reviewing. You can keep chatting.")
@bot.callback_query_handler(func=lambda c: c.data.startswith("exrep:"))
def cb_expartner_report(call):
    """Report after partner left chat"""
    uid = call.from_user.id
    try:
        ex_partner_id = int(call.data.split(":")[1])
    except:
        bot.answer_callback_query(call.id, "Invalid.", show_alert=True)
        return

    # remove the inline button so they can't spam report
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

    # show report reason keyboard
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔀 Spam", callback_data=f"exreptype:{ex_partner_id}:spam"),
        types.InlineKeyboardButton("🚫 Unwanted Content", callback_data=f"exreptype:{ex_partner_id}:unwanted"),
        types.InlineKeyboardButton("😠 Inappropriate Messages", callback_data=f"exreptype:{ex_partner_id}:inappropriate"),
        types.InlineKeyboardButton("🤔 Suspicious Activity", callback_data=f"exreptype:{ex_partner_id}:suspicious"),
        types.InlineKeyboardButton("❓ Other", callback_data=f"exreptype:{ex_partner_id}:other"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="exreptype:cancel"),
    )
    bot.answer_callback_query(call.id)
    bot.send_message(uid, "🚩 Why are you reporting?", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("exreptype:"))
def cb_expartner_report_type(call):
    uid = call.from_user.id
    parts = call.data.split(":")

    if parts[1] == "cancel":
        bot.answer_callback_query(call.id, "Report cancelled.")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        return

    try:
        ex_partner_id = int(parts[1])
        rtype = parts[2]
    except:
        bot.answer_callback_query(call.id, "Invalid.", show_alert=True)
        return

    type_map = {
        "spam": "Spam", "unwanted": "Unwanted Content",
        "inappropriate": "Inappropriate Messages",
        "suspicious": "Suspicious Activity", "other": "Other",
    }
    rtype_name = type_map.get(rtype, "Other")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

    db_add_report(uid, ex_partner_id, rtype_name, "post-chat report")
    forward_to_admin(uid, ex_partner_id, f"{rtype_name} (after chat ended)")
    bot.answer_callback_query(call.id, "Report submitted!")
    bot.send_message(uid, "✅ Report submitted! Thank you for keeping FenLiX safe. 🧹")

# ============================================
# GAMES
# ============================================

@bot.message_handler(commands=["game"])
def cmd_game(message):
    uid = message.from_user.id
    if uid not in active_pairs:
        bot.send_message(uid, "❌ Must be in a chat to start a game.")
        return
    if uid in games:
        bot.send_message(uid, "⚠️ Already in a game! Use /endgame first.")
        return
    partner = active_pairs[uid]
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔢 Guess the Number (1-10)", callback_data=f"gc:guess:{partner}"),
        types.InlineKeyboardButton("📝 Word Chain", callback_data=f"gc:word:{partner}"),
    )
    bot.send_message(uid, "🎮 Choose a game:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("gc:"))
def cb_game_choice(call):
    uid = call.from_user.id
    try:
        _, gtype, pid_str = call.data.split(":")
        partner = int(pid_str)
    except:
        bot.answer_callback_query(call.id, "Invalid selection.", show_alert=True)
        return
    if active_pairs.get(uid) != partner:
        bot.answer_callback_query(call.id, "Partner changed or disconnected.", show_alert=True)
        return
    if uid in games or partner in games:
        bot.answer_callback_query(call.id, "Already in a game!", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if gtype == "guess":
        start_guess_game(uid, partner)
    else:
        start_word_chain(uid, partner)

@bot.message_handler(commands=["word"])
def cmd_word(message):
    uid = message.from_user.id
    state = games.get(uid)
    if not state or state["type"] != "word":
        bot.send_message(uid, "❌ Not in a Word Chain game. Use /game to start one.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(uid, "Usage: /word <yourword>\nExample: /word apple")
        return
    handle_word_move(uid, parts[1].strip())

@bot.message_handler(commands=["endgame"])
def cmd_endgame(message):
    uid = message.from_user.id
    if uid not in games:
        bot.send_message(uid, "❌ Not in any game.")
        return
    state = games.pop(uid, None)
    # find the other player
    other = None
    for k, v in list(games.items()):
        if v is state:
            other = k
            games.pop(k, None)
            break
    if other:
        try:
            bot.send_message(other, "🎮 Your partner ended the game. You can keep chatting!")
        except:
            pass
    bot.send_message(uid, "🎮 Game ended. Keep chatting!")

# ============================================
# ADMIN COMMANDS
# ============================================

@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /ban <id|@username> [hours|permanent] [reason]")
        return
    target = resolve_user(parts[1])
    if not target:
        bot.reply_to(message, f"User not found: {parts[1]}")
        return
    permanent = len(parts) >= 3 and parts[2].lower() == "permanent"
    hours = 24
    if len(parts) >= 3 and not permanent:
        try:
            hours = int(parts[2])
        except:
            pass
    reason = " ".join(parts[3:]) if len(parts) >= 4 else "Banned by admin"
    db_ban(target, hours=None if permanent else hours, permanent=permanent, reason=reason)
    disconnect_user(target, notify_partner=True, reason="banned")
    try:
        bot.send_message(target,
            f"🚫 You have been {'permanently ' if permanent else ''}banned.\nReason: {reason}")
    except:
        pass
    bot.reply_to(message,
        f"✅ {'Permanently' if permanent else f'{hours}h'} banned user {target}.\nReason: {reason}")

@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /unban <id|@username>")
        return
    target = resolve_user(parts[1])
    if not target:
        bot.reply_to(message, f"User not found: {parts[1]}")
        return
    db_unban(target)
    user_warnings[target] = 0
    bot.reply_to(message, f"✅ User {target} unbanned.")
    try:
        bot.send_message(target, "✅ Your ban has been lifted! Welcome back.", reply_markup=main_kb(target))
    except:
        pass

@bot.message_handler(commands=["pradd"])
def cmd_pradd(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /pradd <id> YYYY-MM-DD")
        return
    target = resolve_user(parts[1])
    if not target:
        bot.reply_to(message, f"User not found: {parts[1]}")
        return
    if not db_set_premium(target, parts[2]):
        bot.reply_to(message, "Invalid date. Use YYYY-MM-DD")
        return
    bot.reply_to(message, f"✅ Premium added for {target} until {parts[2]}")
    try:
        bot.send_message(target,
            f"🎉 PREMIUM ACTIVATED!\nValid until {parts[2]}\n♀️ Opposite gender search unlocked!",
            reply_markup=main_kb(target))
    except:
        pass

@bot.message_handler(commands=["prrem"])
def cmd_prrem(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /prrem <id>")
        return
    target = resolve_user(parts[1])
    if not target:
        bot.reply_to(message, f"User not found: {parts[1]}")
        return
    db_remove_premium(target)
    bot.reply_to(message, f"✅ Premium removed for {target}")
    try:
        bot.send_message(target, "⚠️ Your premium has been removed.", reply_markup=main_kb(target))
    except:
        pass

@bot.message_handler(commands=["stats"])
def cmd_botstats(message):
    """Admin only — bot memory + DB snapshot, zero extra RAM"""
    if message.from_user.id != ADMIN_ID:
        return

    # ── Master DB stats from laptop (authoritative) ──────────────
    laptop = get_laptop_stats()           # None if laptop offline
    lc = local_counts()                   # local cache snapshot
    qstats = get_queue_stats()            # pending sync queue

    # ── Runtime RAM dicts ────────────────────────────────────────
    active_chats   = len(active_pairs) // 2        # pairs, not individuals
    queue_random   = len(waiting_random)
    queue_opp      = len(waiting_opposite)
    active_games   = len(games) // 2               # same dict stored twice
    rep_locks      = len(report_reason_pending)
    hist_users     = len(chat_history)
    warn_tracked   = len(user_warnings)
    pending_c      = len(pending_country)

    try:
        import resource
        ram_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        ram_str = f"{ram_mb:.1f} MB (peak RSS)"
    except Exception:
        ram_str = "N/A (use Render dashboard)"

    if laptop:
        master = (
            "💾 MASTER DB (laptop) ✅\n"
            f"  Total users    : {laptop.get('total_users', 0)}\n"
            f"  Premium active : {laptop.get('premium_users', 0)}\n"
            f"  Temp bans      : {laptop.get('temp_bans', 0)}\n"
            f"  Permanent bans : {laptop.get('permanent_bans', 0)}\n"
            f"  Total reports  : {laptop.get('total_reports', 0)}\n"
            f"  Total messages : {laptop.get('total_messages', 0):,}\n"
        )
    else:
        master = "💾 MASTER DB (laptop) ❌ OFFLINE — showing cache only\n"

    msg = (
        "📊 BOT STATS\n"
        "━━━━━━━━━━━━━━━━\n\n"
        + master + "\n"

        "🗃 RENDER CACHE\n"
        f"  Cached users   : {lc['cached_users']}\n"
        f"  Cached bans    : {lc['cached_bans']}\n"
        f"  Cached premium : {lc['cached_premium']}\n\n"

        "🔁 SYNC QUEUE\n"
        f"  Pending events : {qstats['pending']}\n"
        f"  Syncing events : {qstats['syncing']}\n"
        f"  Oldest pending : {qstats['oldest_event'] or '-'}\n\n"

        "💬 LIVE ACTIVITY\n"
        f"  Active chats   : {active_chats} pairs\n"
        f"  Queue (random) : {queue_random}\n"
        f"  Queue (opp)    : {queue_opp}\n"
        f"  Active games   : {active_games}\n\n"

        "🧠 RAM (runtime dicts)\n"
        f"  chat_history   : {hist_users} users\n"
        f"  warnings       : {warn_tracked}\n"
        f"  report locks   : {rep_locks}\n"
        f"  pending country: {pending_c}\n"
        f"  Process RAM    : {ram_str}\n"
    )

    bot.reply_to(message, msg)


@bot.message_handler(commands=["rules"])
def cmd_rules(message):
    bot.send_message(message.from_user.id,
        "📋 CHAT RULES\n\n"
        "1️⃣ Be respectful. No harassment or hate speech.\n"
        "2️⃣ No adult/explicit content.\n"
        "3️⃣ No spam or self-promotion links.\n"
        "4️⃣ Don't share personal info.\n"
        "5️⃣ Violations = warnings then ban.\n\n"
        "🚩 Use /report for bad behavior. Stay safe! 💬")

@bot.message_handler(commands=["help"])
def cmd_help(message):
    uid = message.from_user.id
    bot.send_message(uid,
        "📖 HELP & COMMANDS\n\n"
        "🔍 SEARCH\n"
        "/search_random — Find random partner\n"
        "/search_opposite — Opposite gender (Premium)\n\n"
        "💬 CHAT\n"
        "/next — Skip to new partner\n"
        "/stop — Exit chat/search\n\n"
        "🎮 GAMES (while in a chat)\n"
        "/game — Start a game with partner\n"
        "  🔢 Guess the Number — Partner guesses 1-10\n"
        "  📝 Word Chain — Chain words by last letter\n"
        "/word <word> — Play your Word Chain move\n"
        "/endgame — End current game\n\n"
        "👤 PROFILE\n"
        "/settings — View & edit profile\n"
        "/refer — Get referral link\n\n"
        "⚠️ OTHER\n"
        "/report — Report current partner\n"
        "/rules — Community guidelines\n\n"
        f"🎁 PREMIUM: Invite {PREMIUM_REFERRALS_NEEDED} friends → {PREMIUM_DURATION_HOURS}h premium",
        reply_markup=main_kb(uid))

# ============================================
# MAIN TEXT HANDLER
# ============================================

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"), content_types=["text"])
def handler_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if db_is_banned(uid):
        bot.send_message(uid, "🚫 You are banned.")
        return

    db_create_user(message.from_user)
    u = db_get_user(uid)

    if not u or not u["gender"]:
        bot.send_message(uid, "Set your gender first! Use /start")
        return

    # ── 1. REPORT REASON (chat locked) ──
    if uid in report_reason_pending:
        partner_id = report_reason_pending[uid]
        if text.lower() == "cancel":
            report_reason_pending.pop(uid)
            bot.send_message(uid, "❌ Report cancelled. Chat resumed.", reply_markup=chat_kb())
        elif text.strip():
            db_add_report(uid, partner_id, "Other", text)
            forward_to_admin(uid, partner_id, "Other", text)
            report_reason_pending.pop(uid)
            bot.send_message(uid, "✅ Report submitted! Keep chatting.", reply_markup=chat_kb())
        else:
            bot.send_message(uid, "⛔ Chat locked. Type your reason or 'cancel'.")
        return  # always block forwarding during report

    # ── 2. PENDING COUNTRY ──
    if uid in pending_country:
        info = get_country_info(text)
        if info:
            name, flag = info
            db_update(uid, country=name, country_flag=flag)
            pending_country.discard(uid)
            bot.send_message(uid, f"✅ Country set to {flag} {name}!", reply_markup=main_kb(uid))
        else:
            bot.send_message(uid, f"❌ '{text}' not recognized. Try again (e.g. India):")
        return

    # ── 3. GAME: number input for guesser ──
    state = games.get(uid)
    if state and state["type"] == "guess" and uid == state["guesser"]:
        if handle_guess_input(uid, text):
            return  # consumed

    # ── 4. MENU BUTTONS ──
    if text == "🔀 Search Random":
        cmd_search_random(message); return
    if text == "♀️ Search Opposite Gender":
        cmd_search_opposite(message); return
    if text == "♀️ Opposite Gender (Premium)":
        bot.send_message(uid, "💎 Premium required! Use /refer to earn it free."); return
    if text == "⚙️ Settings":
        cmd_settings(message); return
    if text == "🔗 Refer":
        cmd_refer(message); return
    if text == "📖 Help":
        cmd_help(message); return
    if text == "📊 Stats":
        u = db_get_user(uid)
        ps = "Premium Active ⭐" if db_is_premium(uid) else "Free"
        g = "♂️" if u["gender"] == "Male" else "♀️"
        bot.send_message(uid,
            f"📊 YOUR STATS\n\n"
            f"{g} {u['gender']} | 🎂 {u['age']} | {u['country_flag']} {u['country']}\n\n"
            f"💬 Messages: {u['messages_sent']}\n"
            f"👥 Referred: {u['referral_count']}\n\n"
            f"🎁 {ps}", reply_markup=chat_kb())
        return
    if text == "🚩 Report":
        cmd_report(message); return
    if text == "⏭️ Next":
        cmd_next(message); return
    if text == "🛑 Stop":
        cmd_stop(message); return

    # ── 5. BANNED CONTENT ──
    if is_banned_content(text):
        warn_user(uid, "Inappropriate content or links")
        return

    # ── 6. FORWARD TO PARTNER ──
    if uid in active_pairs:
        partner = active_pairs[uid]
        append_history(uid, message.chat.id, message.message_id)
        try:
            bot.send_message(partner, text)
            db_count_message(uid)
        except Exception as e:
            logger.error(f"Forward error: {e}")
            bot.send_message(uid, "❌ Could not send message. Partner may have left.")
    else:
        bot.send_message(uid, "💬 You're not in a chat.\nTap /search to find a partner.", reply_markup=main_kb(uid))

# ============================================
# MEDIA HANDLER
# ============================================

@bot.message_handler(content_types=["photo","document","video","animation","sticker","audio","voice"])
def handle_media(message):
    """FenLiX is text-only — no media allowed, not even stickers"""
    uid = message.from_user.id
    if db_is_banned(uid):
        return
    if uid not in active_pairs:
        return
    bot.send_message(uid, "🚫 Only text messages are allowed on FenLiX.")

# ============================================
# /msg  — ADMIN BROADCAST  (memory-safe, batched)
# ============================================
#
# Usage:  /msg Hello everyone! Big update coming soon 🎉
#         Everything after /msg is sent to ALL registered users.
#
# How it works:
#   - Users fetched in small DB batches (50 at a time) → no full list in RAM
#   - 0.05s sleep between each send → avoids Telegram flood limits
#   - Failed sends (blocked bot / deleted account) silently skipped
#   - Admin gets a summary at the end: sent / failed counts
#
# Normal users: /msg does nothing (treated as regular chat text)
# ============================================
# /msg  — ADMIN BROADCAST
# ============================================
# Admin: /msg <text>  → sab registered users ko bhejta hai
# Normal user: /msg kare to kuch nahi hota, silently ignore
#
# Memory policy:
#   - Broadcast text sirf thread ke andar local variable hai
#   - DB se 50 IDs at a time fetch → poori list kabhi RAM mein nahi
#   - Send ke baad koi storage nahi — RAM free ho jaati hai
#   - Thread khatam hote hi broadcast_text bhi garbage collected
# ============================================

@bot.message_handler(commands=["msg"])
def cmd_broadcast(message):
    uid = message.from_user.id

    # Non-admin → kuch nahi hoga, bilkul ignore
    if uid != ADMIN_ID:
        return

    # /msg ke baad ka pura text extract karo
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(message,
            "Usage: /msg <apna message>\n\n"
            "Example: /msg Welcome! Bot update aa gaya 🎉")
        return

    # Broadcast text — sirf is scope mein, koi DB mein save nahi
    broadcast_text = parts[1].strip()

    # Users from the local cache (warmed from laptop on startup)
    lc = local_counts()
    total = lc["cached_users"]

    bot.reply_to(message,
        f"📢 Broadcast shuru...\n"
        f"👥 ~{total} cached users ko jayega\n\n"
        f"Message:\n{broadcast_text}")

    # --- BACKGROUND THREAD ---
    # broadcast_text yahan capture hoti hai closure mein
    # thread khatam hote hi automatically garbage collected
    def do_broadcast(text):
        import time
        sent = 0
        failed = 0
        offset = 0
        BATCH = 50  # 50 IDs at a time → RAM safe

        while True:
            rows = iter_user_ids(BATCH, offset)
            if not rows:
                break  # sab ho gaye

            for to_uid in rows:
                if to_uid == ADMIN_ID:
                    continue
                try:
                    bot.send_message(to_uid, text)
                    sent += 1
                except Exception:
                    failed += 1  # block/deleted → skip

                time.sleep(0.05)  # Telegram flood safe ~20/sec

            offset += BATCH
            # rows list yahan scope se bahar — GC free kar deta hai

        # text variable bhi thread khatam hote hi free
        # Admin ko sirf numbers bhejna hai, text dobara nahi
        try:
            bot.send_message(
                ADMIN_ID,
                f"✅ Broadcast done!\n"
                f"📨 Sent: {sent}\n"
                f"❌ Failed: {failed}\n"
                f"👥 Total: {sent + failed}"
            )
        except:
            pass

    # text as argument pass karo — closure capture nahi, clean handoff
    t = threading.Thread(target=do_broadcast, args=(broadcast_text,), daemon=True)
    t.start()
    # broadcast_text ab is function ke scope mein hai, thread ke paas copy
    # function return hone ke baad local var free, thread apna kaam karta rahe


# ============================================
# COMMANDS SETUP
# ============================================

def setup_commands():
    cmds = [
        types.BotCommand("start", "Start & setup profile"),
        types.BotCommand("search", "Find random chat partner"),
        types.BotCommand("search_opposite", "Opposite gender (Premium)"),
        types.BotCommand("next", "Skip to next partner"),
        types.BotCommand("stop", "Stop chatting/searching"),
        types.BotCommand("game", "Start a game with partner"),
        types.BotCommand("word", "Play your Word Chain move"),
        types.BotCommand("endgame", "End current game"),
        types.BotCommand("settings", "Edit your profile"),
        types.BotCommand("refer", "Get referral link"),
        types.BotCommand("report", "Report current partner"),
        types.BotCommand("rules", "Community guidelines"),
        types.BotCommand("help", "Help & all commands"),
    ]
    bot.set_my_commands(cmds)
    try:
        admin_cmds = cmds + [
            types.BotCommand("ban", "Ban a user"),
            types.BotCommand("unban", "Unban a user"),
            types.BotCommand("pradd", "Add premium to user"),
            types.BotCommand("prrem", "Remove user premium"),
            types.BotCommand("msg", "Broadcast to all users"),
            types.BotCommand("stats", "Bot memory + DB stats"),
        ]
        bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
    except:
        pass

# ============================================
# MAIN
# ============================================

# ============================================
# MEMORY CLEANUP THREAD — har 30 min
# ============================================

def run_cleanup():
    """
    RAM cleanup  (har 30 min):
      1. chat_history       — sirf live users ki rakho
      2. report_pending     — stale lock hata do
      3. pending_country    — set ho gayi ya user nahi
      4. games              — orphan game hata do
      5. user_warnings      — banned user ke counts

    CACHE cleanup  (har 30 min):
      6. ban_cache          — expired temp bans hata do (auto-unban)

    NOTE: permanent DB cleanup (incomplete users / old reports) ab LAPTOP
    karta hai — Render ke paas permanent data hai hi nahi.
    """
    import time
    INTERVAL = 30 * 60  # 30 minutes

    while True:
        time.sleep(INTERVAL)
        try:
            # ── RAM cleanup ──────────────────────────────────
            live = (set(active_pairs.keys())
                    | set(waiting_random)
                    | {u for u, _ in waiting_opposite})

            # 1. chat_history
            before_h = len(chat_history)
            for uid in [k for k in list(chat_history.keys()) if k not in live]:
                del chat_history[uid]
            for uid in list(chat_history.keys()):
                if len(chat_history[uid]) > 50:
                    chat_history[uid] = chat_history[uid][-50:]

            # 2. stale report locks
            before_r = len(report_reason_pending)
            for uid in [k for k in list(report_reason_pending.keys())
                        if k not in active_pairs]:
                del report_reason_pending[uid]

            # 3. pending_country
            before_c = len(pending_country)
            for uid in [u for u in list(pending_country)
                        if not db_get_user(u) or (db_get_user(u) or {}).get("country")]:
                pending_country.discard(uid)

            # 4. orphan games
            before_g = len(games)
            for uid in [k for k in list(games.keys()) if k not in active_pairs]:
                games.pop(uid, None)

            # 5. warnings for banned users
            before_w = len(user_warnings)
            for uid in [k for k in list(user_warnings.keys()) if db_is_banned(uid)]:
                del user_warnings[uid]

            ram_freed = {
                "history": before_h - len(chat_history),
                "rep_locks": before_r - len(report_reason_pending),
                "country_q": before_c - len(pending_country),
                "games": before_g - len(games),
                "warnings": before_w - len(user_warnings),
            }

            # ── CACHE cleanup ────────────────────────────────
            # expired temp bans cache se hata do (auto-unban)
            purge_expired_bans()

            # log only what changed
            all_freed = {k: v for k, v in ram_freed.items() if v > 0}
            if all_freed:
                logger.info(
                    f"[CLEANUP] Freed: {all_freed} | "
                    f"Active: {len(active_pairs)} pairs | "
                    f"Queue: {len(waiting_random)+len(waiting_opposite)}"
                )
            else:
                logger.info(
                    f"[CLEANUP] Nothing to clean | "
                    f"Active: {len(active_pairs)} pairs"
                )

        except Exception as e:
            logger.error(f"[CLEANUP] Error: {e}")


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("FenLiX Bot v7.0 (distributed) starting...")
    logger.info("Render = temp queue + cache | Laptop = master DB")
    logger.info("=" * 50)

    # 1. local queue/cache + warm from laptop + start background sync
    init_distributed_system()
    # 2. let the sync engine trigger user-facing messages (ban/premium)
    set_notify_callback(on_side_effect)

    logger.info("Setting up commands...")
    setup_commands()

    # cache bot username once at startup
    get_bot_username()

    # Memory cleanup background thread
    threading.Thread(target=run_cleanup, daemon=True).start()

    # Flask — Render ke liye
    port = int(os.getenv("PORT", 5000))
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True
    ).start()
    logger.info(f"Flask running on port {port}")

    # Telegram polling
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
