#!/usr/bin/env python3
"""
GhostTalk Premium Bot - FIXED v6.0

Fixes applied:
- Game logic properly fixed (guess + word chain both work)
- Admin notification on gender/settings change REMOVED
- disconnect_user now notifies partner
- Media consent flow: partner asked before receiving (stickers bypass)
- Report lock: duplicate check removed, clean flow
- Referral count: fixed (atomic read after write)
- /word command added for word chain
- Game state: no shared-reference bugs
- Country change: open to all users (no premium lock)
"""

import os
import re
import sqlite3
import random
import threading
import logging
from datetime import datetime, timedelta, timezone
import telebot
from telebot import types
from flask import Flask

# ============================================
# CONFIG
# ============================================

# python bot.py

BASEDIR = os.getcwd()
DATA_PATH = os.getenv("DATA_PATH") or os.path.join(BASEDIR, "data")
os.makedirs(DATA_PATH, exist_ok=True)
DB_PATH = os.getenv("DB_PATH") or os.path.join(DATA_PATH, "ghosttalk.db")

API_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") 
ADMIN_ID = int(os.getenv("ADMIN_ID"))

WARNING_LIMIT = 2
TEMP_BAN_HOURS = 24
PREMIUM_REFERRALS_NEEDED = 3
PREMIUM_DURATION_HOURS = 1

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

def get_conn():
    parent = os.path.dirname(DB_PATH) or BASEDIR
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                gender TEXT,
                age INTEGER,
                country TEXT,
                country_flag TEXT,
                messages_sent INTEGER DEFAULT 0,
                media_approved INTEGER DEFAULT 0,
                media_rejected INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referral_count INTEGER DEFAULT 0,
                premium_until TEXT,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                ban_until TEXT,
                permanent INTEGER DEFAULT 0,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                reported_id INTEGER,
                report_type TEXT,
                reason TEXT,
                timestamp TEXT
            );
        """)
        c.commit()

def db_get_user(uid):
    with get_conn() as c:
        row = c.execute(
            "SELECT user_id,username,first_name,gender,age,country,country_flag,"
            "messages_sent,media_approved,media_rejected,referral_code,referral_count,premium_until "
            "FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    if not row:
        return None
    keys = ["user_id","username","first_name","gender","age","country","country_flag",
            "messages_sent","media_approved","media_rejected","referral_code","referral_count","premium_until"]
    return dict(zip(keys, row))

def db_create_user(tg_user):
    if db_get_user(tg_user.id):
        return
    code = f"REF{tg_user.id}{random.randint(1000,99999)}"
    with get_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id,username,first_name,gender,age,country,country_flag,joined_at,referral_code) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tg_user.id, tg_user.username or "", tg_user.first_name or "",
             None, None, None, None, datetime.now(timezone.utc).isoformat(), code)
        )
        c.commit()

def db_update(uid, **fields):
    if not fields:
        return
    # handle None values
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), uid))
        c.commit()

def db_is_premium(uid):
    if uid == ADMIN_ID:
        return True
    u = db_get_user(uid)
    if not u or not u["premium_until"]:
        return False
    try:
        pu = datetime.fromisoformat(u["premium_until"])
        return pu > datetime.now(timezone.utc).replace(tzinfo=None)
    except:
        return False

def db_set_premium(uid, until_str):
    try:
        s = f"{until_str}T23:59:59" if len(until_str) == 10 else until_str
        dt = datetime.fromisoformat(s)
        db_update(uid, premium_until=dt.isoformat())
        return True
    except:
        return False

def db_is_banned(uid):
    if uid == ADMIN_ID:
        return False
    with get_conn() as c:
        row = c.execute("SELECT ban_until,permanent FROM bans WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return False
    ban_until, permanent = row
    if permanent:
        return True
    if ban_until:
        try:
            return datetime.fromisoformat(ban_until) > datetime.now(timezone.utc).replace(tzinfo=None)
        except:
            return False
    return False

def db_ban(uid, hours=None, permanent=False, reason=""):
    with get_conn() as c:
        if permanent:
            c.execute("INSERT OR REPLACE INTO bans VALUES (?,?,?,?)", (uid, None, 1, reason))
        else:
            until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat() if hours else None
            c.execute("INSERT OR REPLACE INTO bans VALUES (?,?,?,?)", (uid, until, 0, reason))
        c.commit()

def db_unban(uid):
    with get_conn() as c:
        c.execute("DELETE FROM bans WHERE user_id=?", (uid,))
        c.commit()

def db_add_report(reporter_id, reported_id, report_type, reason):
    ts = datetime.now(timezone.utc).isoformat()
    with get_conn() as c:
        c.execute(
            "INSERT INTO reports (reporter_id,reported_id,report_type,reason,timestamp) VALUES (?,?,?,?,?)",
            (reporter_id, reported_id, report_type, reason, ts)
        )
        count = c.execute("SELECT COUNT(*) FROM reports WHERE reported_id=?", (reported_id,)).fetchone()[0]
        c.commit()

    # auto-ban at 10 reports
    if count >= 10 and not db_is_banned(reported_id):
        db_ban(reported_id, hours=168, reason="Auto-banned: 10+ reports")
        dt_str = datetime.fromisoformat(ts).strftime("%Y-%m-%d at %H:%M")
        with get_conn() as c:
            reporters = c.execute(
                "SELECT DISTINCT reporter_id FROM reports WHERE reported_id=?", (reported_id,)
            ).fetchall()
        for (rid,) in reporters:
            try:
                bot.send_message(rid,
                    f"✅ Action Taken!\nReport reviewed & action taken on {dt_str}\n"
                    "Thanks for keeping our community clean! 🧹")
            except:
                pass

def db_add_referral(referrer_id):
    # atomic: increment then read in same connection
    with get_conn() as c:
        c.execute("UPDATE users SET referral_count=referral_count+1 WHERE user_id=?", (referrer_id,))
        c.commit()
        count = c.execute("SELECT referral_count FROM users WHERE user_id=?", (referrer_id,)).fetchone()[0]

    if count >= PREMIUM_REFERRALS_NEEDED:
        until = (datetime.now(timezone.utc) + timedelta(hours=PREMIUM_DURATION_HOURS)).isoformat()
        db_update(referrer_id, premium_until=until, referral_count=0)
        try:
            bot.send_message(referrer_id,
                f"🎉 PREMIUM UNLOCKED!\n{PREMIUM_DURATION_HOURS}h premium earned!\n"
                "♀️ Opposite gender search unlocked!")
        except:
            pass

def db_get_referral_link(uid):
    u = db_get_user(uid)
    if not u:
        return None
    try:
        uname = bot.get_me().username
        return f"https://t.me/{uname}?start={u['referral_code']}"
    except:
        return f"REFCODE:{u['referral_code']}"

def resolve_user(identifier):
    if not identifier:
        return None
    try:
        return int(identifier.strip())
    except:
        pass
    uname = identifier.strip().lstrip("@")
    with get_conn() as c:
        row = c.execute("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)", (uname,)).fetchone()
    return row[0] if row else None

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

def disconnect_user(uid, notify_partner=True):
    partner = active_pairs.pop(uid, None)
    if partner:
        active_pairs.pop(partner, None)
        # cleanup games
        games.pop(uid, None)
        games.pop(partner, None)
        if notify_partner:
            try:
                bot.send_message(partner,
                    "👋 Partner disconnected.\n\nUse 🔀 Search Random to find someone new!",
                    reply_markup=main_kb(partner))
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
        disconnect_user(uid)
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
    return "GhostTalk Bot Running!", 200

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
# STEP 3 — Uncomment karo sirf ye route:
#
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
        with get_conn() as c:
            row = c.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
        if row and row[0] != uid:
            db_add_referral(row[0])
            bot.send_message(uid, "✅ You joined via a referral link!")

    u = db_get_user(uid)

    if not u or not u["gender"]:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("♂️ Male", callback_data="sex:male"),
            types.InlineKeyboardButton("♀️ Female", callback_data="sex:female")
        )
        bot.send_message(uid,
            "🌐 Welcome to GhostTalk!\n\nAnonymous chat platform.\nSelect your gender to get started:",
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
        f"📸 Media Sent: {u['media_approved']}\n"
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

@bot.message_handler(commands=["search_random"])
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
    if not in_q:
        # naya add karo queue mein
        remove_from_queues(uid)
        waiting_random.append(uid)
        bot.send_message(uid, "🔍 Searching for a random partner...")
    # hamesha match_users call karo - chahe pehle se queue mein ho ya abhi add kiya
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
    if not in_q:
        remove_from_queues(uid)
        waiting_opposite.append((uid, u["gender"]))
        bot.send_message(uid, "🔍 Searching for opposite gender partner...")
    # hamesha match karo
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
    dt_str = datetime.now(timezone.utc).strftime("%Y-%m-%d at %H:%M")
    with get_conn() as c:
        reporters = c.execute("SELECT DISTINCT reporter_id FROM reports WHERE reported_id=?", (target,)).fetchall()
    for (rid,) in reporters:
        try:
            bot.send_message(rid,
                f"✅ Action Taken!\nReport reviewed on {dt_str}\n"
                "Thanks for keeping our community clean! 🧹")
        except:
            pass
    disconnect_user(target, notify_partner=True)
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
    db_update(target, premium_until=None)
    bot.reply_to(message, f"✅ Premium removed for {target}")
    try:
        bot.send_message(target, "⚠️ Your premium has been removed.", reply_markup=main_kb(target))
    except:
        pass

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
            f"📸 Media Sent: {u['media_approved']}\n"
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
            with get_conn() as c:
                c.execute("UPDATE users SET messages_sent=messages_sent+1 WHERE user_id=?", (uid,))
                c.commit()
        except Exception as e:
            logger.error(f"Forward error: {e}")
            bot.send_message(uid, "❌ Could not send message. Partner may have left.")
    else:
        bot.send_message(uid, "Not connected. Use 🔀 Search Random.", reply_markup=main_kb(uid))

# ============================================
# MEDIA HANDLER
# ============================================

@bot.message_handler(content_types=["photo","document","video","animation","sticker","audio","voice"])
def handle_media(message):
    uid = message.from_user.id
    if db_is_banned(uid):
        bot.send_message(uid, "🚫 You are banned.")
        return
    if uid not in active_pairs:
        bot.send_message(uid, "❌ Not in a chat.")
        return
    if uid in report_reason_pending:
        bot.send_message(uid, "⛔ Chat locked during report. Type reason or 'cancel'.")
        return

    partner = active_pairs[uid]
    mtype = message.content_type
    mid_map = {
        "photo": lambda m: m.photo[-1].file_id,
        "document": lambda m: m.document.file_id,
        "video": lambda m: m.video.file_id,
        "animation": lambda m: m.animation.file_id,
        "sticker": lambda m: m.sticker.file_id,
        "audio": lambda m: m.audio.file_id,
        "voice": lambda m: m.voice.file_id,
    }
    media_id = mid_map.get(mtype, lambda m: None)(message)
    if not media_id:
        return

    append_history(uid, message.chat.id, message.message_id)

    # stickers: no consent needed
    if mtype == "sticker":
        try:
            bot.send_sticker(partner, media_id)
            u = db_get_user(uid)
            db_update(uid, media_approved=(u["media_approved"] + 1) if u else 1)
        except:
            bot.send_message(uid, "❌ Could not send sticker.")
        return

    # ask partner for consent
    cb_data = f"media:allow:{uid}:{mtype}:{media_id}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Allow", callback_data=cb_data),
        types.InlineKeyboardButton("❌ Decline", callback_data=f"media:deny:{uid}"),
    )
    try:
        bot.send_message(partner,
            f"📎 Partner wants to send a {mtype}.\nAllow?", reply_markup=markup)
        bot.send_message(uid, "⏳ Waiting for partner to accept your media...")
    except:
        bot.send_message(uid, "❌ Could not request consent.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("media:"))
def cb_media(call):
    parts = call.data.split(":")
    action = parts[1]
    sender_id = int(parts[2])

    if action == "deny":
        bot.answer_callback_query(call.id, "Media declined.")
        try:
            bot.send_message(sender_id, "❌ Partner declined your media.")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        return

    # allow
    mtype = parts[3]
    media_id = ":".join(parts[4:])
    bot.answer_callback_query(call.id, "Media accepted!")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

    send_map = {
        "photo": bot.send_photo, "document": bot.send_document,
        "video": bot.send_video, "animation": bot.send_animation,
        "audio": bot.send_audio, "voice": bot.send_voice,
    }
    fn = send_map.get(mtype)
    if fn:
        try:
            fn(call.from_user.id, media_id)
            u = db_get_user(sender_id)
            if u:
                db_update(sender_id, media_approved=u["media_approved"] + 1)
            bot.send_message(sender_id, "✅ Media delivered!")
        except Exception as e:
            logger.error(f"Media deliver error: {e}")
            bot.send_message(sender_id, "❌ Could not deliver media.")

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

    # Kitne users hain — lightweight COUNT query
    with get_conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM users WHERE user_id != ?", (ADMIN_ID,)
        ).fetchone()[0]

    bot.reply_to(message,
        f"📢 Broadcast shuru...\n"
        f"👥 {total} users ko jayega\n\n"
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
            # Sirf user_id fetch karo, koi extra data nahi
            with get_conn() as c:
                rows = c.execute(
                    "SELECT user_id FROM users WHERE user_id != ?"
                    " LIMIT ? OFFSET ?",
                    (ADMIN_ID, BATCH, offset)
                ).fetchall()

            if not rows:
                break  # sab ho gaye

            for (to_uid,) in rows:
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
        types.BotCommand("search_random", "Find random chat partner"),
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
            types.BotCommand("msg", "Broadcast message to all users"),
        ]
        bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
    except:
        pass

# ============================================
# MAIN
# ============================================

if __name__ == "__main__":
    logger.info("Initializing DB...")
    init_db()
    logger.info("Setting up commands...")
    setup_commands()
    logger.info("=" * 50)
    logger.info("GhostTalk Bot v6.0 FINAL starting...")
    logger.info("✅ Game logic fixed (Guess + Word Chain)")
    logger.info("✅ Admin notify on settings REMOVED")
    logger.info("✅ disconnect_user notifies partner")
    logger.info("✅ Media consent flow fixed")
    logger.info("✅ Referral atomic read fixed")
    logger.info("✅ /word command added")
    logger.info("✅ /msg admin broadcast (memory-safe)")
    logger.info("✅ Render + UptimeRobot setup (see FLASK section)")
    logger.info("=" * 50)

    # Flask server — needed for Render deployment (keeps dyno alive)
    # PORT env var auto-set by Render; locally defaults to 5000
    port = int(os.getenv("PORT", 5000))
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True
    ).start()
    logger.info(f"Flask running on port {port}")

    # Start Telegram polling
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
