"""
bot.py — Telegram bot frontend for api.py backend (loopback, no auth).
No owner restriction — anyone with the bot username can use it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG  (unchanged)
# ============================================================

_DEFAULT_BOT_TOKEN   = "9268658834:AAFIyVOtk8HRKqm_v-MHrNxIaDeyjBZau48"
_DEFAULT_BACKEND_URL = "http://127.0.0.1:11584"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", _DEFAULT_BOT_TOKEN).strip()
BACKEND_URL        = os.environ.get("BACKEND_URL", _DEFAULT_BACKEND_URL).strip().rstrip("/")

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN missing or malformed", file=sys.stderr); sys.exit(1)
if not BACKEND_URL.startswith(("http://", "https://")):
    print(f"ERROR: BACKEND_URL must start with http(s)://, got {BACKEND_URL!r}",
          file=sys.stderr); sys.exit(1)

TIMEOUT        = 30.0
BACKUP_TIMEOUT = 90.0
VOICE_TIMEOUT  = 3600.0          # NEW: voice joins can be slow (SSRC retry in backend)
VOICE_CONCURRENCY = 10         # NEW: how many accounts join/leave in parallel

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tg_bot")

print(f"CONFIG | bot_token=***{TELEGRAM_BOT_TOKEN[-6:]} | backend={BACKEND_URL}",
      file=sys.stderr)

# ============================================================
# HTTP
# ============================================================

async def _request(method: str, path: str, json: dict | None = None,
                   timeout: float = TIMEOUT) -> httpx.Response:
    url = f"{BACKEND_URL}{path}"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient() as c:
                if method == "GET":
                    return await c.get(url, timeout=timeout)
                return await c.post(url, json=json or {}, timeout=timeout)
        except httpx.ConnectError:
            if attempt == 1:
                log.warning("ConnectError to %s — retrying", url)
                await asyncio.sleep(1.0)
                continue
            raise


async def api_get(path: str) -> httpx.Response:
    return await _request("GET", path)


async def api_post(path: str, json: dict | None = None,
                   timeout: float = TIMEOUT) -> httpx.Response:
    return await _request("POST", path, json=json, timeout=timeout)


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))[:250]
    except Exception:
        return resp.text[:250]


def _conn_error_text() -> str:
    return (
        "❌ Cannot reach backend.\n\n"
        f"URL: {BACKEND_URL}\n\n"
        "The backend (api.py) is not running or crashed."
    )

# ============================================================
# UI
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Status"),   KeyboardButton("👥 Accounts")],
        [KeyboardButton("🔐 Login"),    KeyboardButton("🚪 Logout")],
        [KeyboardButton("☁️ Backup"),   KeyboardButton("♻️ Restore")],
        [KeyboardButton("🎙️ Join VC"), KeyboardButton("🔴 Leave VC")],
        [KeyboardButton("❔ Help")],
    ],
    resize_keyboard=True,
)

BUTTON_TEXTS = [
    "📊 Status", "👥 Accounts", "🔐 Login", "🚪 Logout",
    "☁️ Backup", "♻️ Restore", "🎙️ Join VC", "🔴 Leave VC", "❔ Help",
]
BUTTON_PATTERN = "^(" + "|".join(re.escape(t) for t in BUTTON_TEXTS) + ")$"
NOT_BUTTON_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(BUTTON_PATTERN)

# Conversation states
LOGIN_LABEL, LOGIN_PHONE, LOGIN_OTP, LOGIN_2FA = range(4)   # 0..3
JOINVC_LINK,   JOINVC_CONFIRM  = range(4, 6)                # 4,5
LEAVEVC_LINK,  LEAVEVC_CONFIRM = range(6, 8)                # 6,7

# ============================================================
# CHAT REF NORMALIZER  (unchanged)
# ============================================================

def _normalize_chat_ref(raw: str) -> str | None:
    s = raw.strip()
    for prefix in (
        "https://t.me/", "http://t.me/",
        "https://telegram.me/", "http://telegram.me/",
        "t.me/", "telegram.me/",
    ):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "?" in s:
        s = s.split("?", 1)[0]
    s = s.rstrip("/")
    if not s:
        return None
    if s.startswith("+") or s.startswith("joinchat/"):
        return None
    if s.lstrip("-").isdigit():
        return s
    if s.startswith("@"):
        return s
    return f"@{s}"

# ============================================================
# COMMANDS
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    online = False
    try:
        r = await api_get("/health")
        online = r.status_code == 200
    except Exception:
        pass
    await update.message.reply_text(
        "🤖 Telegram Control Bot\n\n"
        "Manage your Telegram accounts and sessions.\n\n"
        f"Backend: {'🟢 online' if online else '🔴 unreachable'}\n\n"
        "Use the buttons below.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❔ Help\n\n"
        "📊 Status — backend health + persistence\n"
        "👥 Accounts — list registered accounts (no limit)\n"
        "🔐 Login — OTP + optional 2FA login flow\n"
        "🚪 Logout — clear an account's session\n"
        "☁️ Backup — save encrypted state to db.json\n"
        "♻️ Restore — reload from db.json (asks confirm)\n"
        "🎙️ Join VC — join **ALL registered accounts** to a group call\n"
        "🔴 Leave VC — remove **ALL registered accounts** from a group call\n"
        "❔ Help — this message\n"
        "/cancel — abort any in-progress flow",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = await api_get("/status")
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text()); return
    except Exception as e:
        await update.message.reply_text(f"❌ Status failed: {type(e).__name__}"); return
    if r.status_code != 200:
        await update.message.reply_text(f"❌ Status failed: {r.status_code} {_detail(r)}"); return
    d = r.json()
    await update.message.reply_text(
        "📊 Service Status\n"
        f"• Service: {d.get('service', '?')}\n"
        f"• Accounts: {d.get('accounts_loaded', 0)}/{d.get('max_accounts', 0)}\n"
        f"• Clients connected: {d.get('clients_connected', 0)}\n"
        f"• Storage ready: {'✅' if d.get('storage_ready') else '❌'}\n"
        f"• Persistence verified: {'✅' if d.get('storage_persistence_verified') else '⚠️ not proven'}\n"
        f"• Startup error: {d.get('startup_error') or 'none'}"
    )


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = await api_get("/accounts")
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text()); return
    except Exception as e:
        await update.message.reply_text(f"❌ Accounts failed: {type(e).__name__}"); return
    if r.status_code != 200:
        await update.message.reply_text(f"❌ Accounts failed: {r.status_code} {_detail(r)}"); return
    accounts = r.json() or []
    if not accounts:
        await update.message.reply_text("📭 No accounts registered.\n\nUse 🔐 Login to add one.")
        return
    lines = []
    for a in accounts:
        lines.append(
            f"• {a.get('account_id', '?')} — {a.get('label') or '(no label)'}\n"
            f"  status: {a.get('status', '?')} | session: {'✅' if a.get('session_available') else '❌'}"
        )
    await update.message.reply_text("👥 Registered Accounts\n\n" + "\n\n".join(lines))


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("☁️ Backup in progress…")
    try:
        r = await api_post("/storage/backup", timeout=BACKUP_TIMEOUT)
    except httpx.ConnectError:
        await msg.edit_text(_conn_error_text()); return
    except Exception as e:
        await msg.edit_text(f"❌ Backup failed: {type(e).__name__}"); return
    if r.status_code != 200:
        await msg.edit_text(f"❌ Backup failed: {r.status_code} {_detail(r)}"); return
    d = r.json()
    await msg.edit_text(
        "✅ Backup complete\n"
        f"• Files: {len(d.get('uploaded_paths', []))}\n"
        f"• Manifest SHA-256: {str(d.get('manifest_sha256', ''))[:16]}…\n"
        f"• Persistence: {'✅ verified' if d.get('persistence_verified') else '⚠️ run ♻️ Restore to prove'}"
    )


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, restore", callback_data="restore_yes"),
        InlineKeyboardButton("❌ Cancel",       callback_data="restore_no"),
    ]])
    await update.message.reply_text(
        "♻️ Restore will overwrite in-memory accounts from db.json.\n\nConfirm?",
        reply_markup=kb,
    )


async def restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.data == "restore_no":
        await q.edit_message_text("❌ Restore cancelled."); return
    await q.edit_message_text("♻️ Restoring from db.json…")
    try:
        r = await api_post("/storage/restore", timeout=BACKUP_TIMEOUT)
    except httpx.ConnectError:
        await q.edit_message_text(_conn_error_text()); return
    except Exception as e:
        await q.edit_message_text(f"❌ Restore failed: {type(e).__name__}"); return
    if r.status_code != 200:
        await q.edit_message_text(f"❌ Restore failed: {r.status_code} {_detail(r)}"); return
    d = r.json()
    await q.edit_message_text(
        "✅ Restore complete\n"
        f"• Accounts restored: {d.get('accounts_restored', 0)}\n"
        f"• Verified: {'✅' if d.get('verified') else '⚠️'}\n"
        f"• Persistence: {'✅ proven' if d.get('storage_persistence_verified') else '⚠️ not proven'}"
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = await api_get("/accounts")
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text()); return
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {type(e).__name__}"); return
    accounts = r.json() if r.status_code == 200 else []
    if not accounts:
        await update.message.reply_text("📭 No accounts to log out."); return
    buttons = [[
        InlineKeyboardButton(
            f"🚪 {a.get('label') or a.get('account_id')} ({a.get('account_id')})",
            callback_data=f"logout_{a.get('account_id')}",
        )
    ] for a in accounts]
    await update.message.reply_text(
        "Which account should log out?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    account_id = q.data.replace("logout_", "", 1)
    try:
        r = await api_post(f"/accounts/{account_id}/logout")
    except httpx.ConnectError:
        await q.edit_message_text(_conn_error_text()); return
    except Exception as e:
        await q.edit_message_text(f"❌ Logout failed: {type(e).__name__}"); return
    if r.status_code != 200:
        await q.edit_message_text(f"❌ Logout failed: {r.status_code} {_detail(r)}"); return
    await q.edit_message_text(f"✅ Logged out {account_id}.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text(
            "🛑 Cancelled.", reply_markup=MAIN_KEYBOARD
        )
    return ConversationHandler.END

# ============================================================
# LOGIN CONVERSATION  (no more account cap)
# ============================================================

async def login_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # No hard-coded limit — the backend enforces its own policy if it wants to.
    await update.message.reply_text(
        "🔐 Login started.\n\nEnter a label for this account (e.g. Primary):"
    )
    return LOGIN_LABEL


async def login_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    label = update.message.text.strip()
    if not label or len(label) > 120:
        await update.message.reply_text("❌ Label must be 1–120 chars. Try again or /cancel.")
        return LOGIN_LABEL
    context.user_data["login_label"] = label
    await update.message.reply_text("📱 Enter phone in international format (+15551234567):")
    return LOGIN_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not re.match(r"^\+\d{5,20}$", phone):
        await update.message.reply_text("❌ Phone must start with + and digits. Try again or /cancel.")
        return LOGIN_PHONE

    label = context.user_data.get("login_label", "")
    account_id = f"acct_{int(time.time() * 1000)}"
    context.user_data["login_account_id"] = account_id

    try:
        r = await api_post("/accounts", {"account_id": account_id, "label": label, "phone": phone})
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Register failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    if r.status_code != 201:
        await update.message.reply_text(f"❌ Register failed: {r.status_code} {_detail(r)}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    try:
        r = await api_post(f"/accounts/{account_id}/login/send_code", {"phone": phone})
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ OTP send failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    if r.status_code != 200:
        await update.message.reply_text(f"❌ OTP send failed: {r.status_code} {_detail(r)}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    await update.message.reply_text("✅ OTP sent by Telegram. Enter the code now:")
    return LOGIN_OTP


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    if not re.match(r"^\d{3,8}$", code):
        await update.message.reply_text("❌ OTP must be digits. Try again or /cancel.")
        return LOGIN_OTP

    account_id = context.user_data.get("login_account_id")
    if not account_id:
        await update.message.reply_text("❌ Session expired. Try 🔐 Login again.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    try:
        r = await api_post(f"/accounts/{account_id}/login/verify", {"code": code})
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Verify failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    if r.status_code == 200:
        await update.message.reply_text("✅ Login complete. Session encrypted and saved.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    detail = _detail(r)
    if r.status_code == 401 and "2FA" in detail:
        context.user_data["login_otp"] = code
        await update.message.reply_text("🔐 2FA password required. Enter it now:")
        return LOGIN_2FA

    await update.message.reply_text(f"❌ Verify failed: {r.status_code} {detail}", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear(); return ConversationHandler.END


async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    if not password:
        await update.message.reply_text("❌ Password empty. Try again or /cancel.")
        return LOGIN_2FA

    account_id = context.user_data.get("login_account_id")
    code = context.user_data.get("login_otp")
    if not account_id or not code:
        await update.message.reply_text("❌ Session expired. Try 🔐 Login again.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    try:
        r = await api_post(f"/accounts/{account_id}/login/verify", {"code": code, "password": password})
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    if r.status_code == 200:
        await update.message.reply_text("✅ 2FA verified. Login complete.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(f"❌ 2FA failed: {r.status_code} {_detail(r)}", reply_markup=MAIN_KEYBOARD)
    context.user_data.clear(); return ConversationHandler.END

# ============================================================
# VOICE HELPERS (join / leave, applied to ALL accounts)
# ============================================================

async def _voice_op_one(account: dict, ref: str, op: str, max_retries: int = 3):
    """
    op = 'join' or 'leave'.  Returns (account_id, label, ok, info).
    Retries SSRC-type failures with back-off so the backend can regenerate
    a fresh SSRC on the next attempt.
    """
    aid = account.get("account_id")
    label = account.get("label") or aid
    payload = {"chat": ref, "muted": True} if op == "join" else {"chat": ref}

    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            r = await api_post(
                f"/accounts/{aid}/voice/{op}", payload, timeout=VOICE_TIMEOUT
            )
            if r.status_code == 200:
                return (aid, label, True, "")
            last_err = f"{r.status_code} {_detail(r)}"[:180]
        except httpx.ConnectError:
            last_err = "connect error"
        except Exception as e:
            last_err = type(e).__name__

        if attempt < max_retries:
            await asyncio.sleep(attempt)   # 1s, 2s back-off

    return (aid, label, False, last_err)


async def _voice_show_results(q, context, ref: str, op: str, results) -> int:
    """Renders join/leave results. Returns the state to stay in (for retry)."""
    verb = "Join" if op == "join" else "Leave"
    icon = "🎙️" if op == "join" else "🔴"
    ok   = [r for r in results if r[2]]
    fail = [r for r in results if not r[2]]

    lines = [
        f"{icon} {verb} results — {ref}",
        f"✅ Success: {len(ok)}/{len(results)}",
    ]
    if not fail:
        context.user_data.clear()
        await q.edit_message_text("\n".join(lines))
        return ConversationHandler.END

    lines.append(f"❌ Failed: {len(fail)}/{len(results)}")
    for aid, label, _, info in fail[:10]:
        lines.append(f"  • {label} ({aid}): {info[:80]}")
    if len(fail) > 10:
        lines.append(f"  …and {len(fail) - 10} more (see backend logs)")

    # Save failures so the retry button can re-attempt only those.
    context.user_data[f"{op}_failed"] = [(aid, label) for aid, label, _, _ in fail]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🔁 Retry failed ({len(fail)})",
                             callback_data=f"{op}_retry"),
    ]])

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…(truncated)"

    await q.edit_message_text(text, reply_markup=kb)
    return JOINVC_CONFIRM if op == "join" else LEAVEVC_CONFIRM


async def _voice_confirm(update, context, op: str) -> int:
    q = update.callback_query
    await q.answer()

    if q.data.endswith("_cancel"):
        context.user_data.clear()
        await q.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    ref = context.user_data.get(f"{op}_link")
    if not ref:
        context.user_data.clear()
        await q.edit_message_text(f"❌ Session expired. Try again.")
        return ConversationHandler.END

    try:
        r = await api_get("/accounts")
    except httpx.ConnectError:
        context.user_data.clear()
        await q.edit_message_text(_conn_error_text())
        return ConversationHandler.END
    except Exception as e:
        context.user_data.clear()
        await q.edit_message_text(f"❌ Failed: {type(e).__name__}")
        return ConversationHandler.END

    accounts = r.json() if r.status_code == 200 else []
    if not accounts:
        context.user_data.clear()
        await q.edit_message_text("❌ No accounts registered.")
        return ConversationHandler.END

    icon = "🎙️" if op == "join" else "🔴"
    verb = "Joining" if op == "join" else "Leaving"
    await q.edit_message_text(
        f"{icon} {verb} {ref} with {len(accounts)} account(s)…\n\n"
        "This may take a minute — please wait."
    )

    sem = asyncio.Semaphore(VOICE_CONCURRENCY)

    async def run_one(a):
        async with sem:
            return await _voice_op_one(a, ref, op)

    results = await asyncio.gather(*[run_one(a) for a in accounts])
    return await _voice_show_results(q, context, ref, op, results)


async def _voice_retry(update, context, op: str) -> int:
    q = update.callback_query
    await q.answer()

    ref = context.user_data.get(f"{op}_link")
    failed = context.user_data.get(f"{op}_failed") or []
    if not ref or not failed:
        context.user_data.clear()
        await q.edit_message_text("❌ Nothing to retry.")
        return ConversationHandler.END

    await q.edit_message_text(f"🔁 Retrying {len(failed)} account(s)…")

    accounts = [{"account_id": aid, "label": label} for aid, label in failed]
    sem = asyncio.Semaphore(VOICE_CONCURRENCY)

    async def run_one(a):
        async with sem:
            return await _voice_op_one(a, ref, op)

    results = await asyncio.gather(*[run_one(a) for a in accounts])
    return await _voice_show_results(q, context, ref, op, results)

# ============================================================
# JOIN VC CONVERSATION  (joins ALL accounts)
# ============================================================

async def joinvc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🎙️ Send the group / VC link.\n\n"
        "Accepted: `https://t.me/groupname`, `@groupname`, `-1001234567890`\n\n"
        "Private invite links (t.me/+abc) are not supported.\n\n"
        "**All registered accounts** will join the call."
    )
    return JOINVC_LINK


async def joinvc_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    ref = _normalize_chat_ref(raw)
    if ref is None:
        await update.message.reply_text(
            "❌ Private invite links are not supported.\n\n"
            "Use a public @username or numeric chat ID.\n"
            "Examples: @mygroup  or  -1001234567890",
        )
        return JOINVC_LINK

    context.user_data["join_link"] = ref

    try:
        r = await api_get("/accounts")
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    accounts = r.json() if r.status_code == 200 else []
    if not accounts:
        await update.message.reply_text("❌ No registered accounts.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Join all {len(accounts)}", callback_data="join_confirm"),
        InlineKeyboardButton("❌ Cancel",                   callback_data="join_cancel"),
    ]])
    await update.message.reply_text(
        f"🎙️ Target: {ref}\n\n"
        f"This will join **ALL {len(accounts)} account(s)** to the call.\n\n"
        "Confirm?",
        reply_markup=kb,
    )
    return JOINVC_CONFIRM


async def joinvc_confirm(update, context):
    return await _voice_confirm(update, context, "join")


async def joinvc_retry(update, context):
    return await _voice_retry(update, context, "join")

# ============================================================
# LEAVE VC CONVERSATION  (removes ALL accounts)
# ============================================================

async def leavevc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🔴 Send the group / VC link to leave.\n\n"
        "Accepted: `https://t.me/groupname`, `@groupname`, `-1001234567890`\n\n"
        "**All registered accounts** will be removed."
    )
    return LEAVEVC_LINK


async def leavevc_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    ref = _normalize_chat_ref(raw)
    if ref is None:
        await update.message.reply_text(
            "❌ Private invite links are not supported.\n\n"
            "Use a public @username or numeric chat ID.",
        )
        return LEAVEVC_LINK

    context.user_data["leave_link"] = ref

    try:
        r = await api_get("/accounts")
    except httpx.ConnectError:
        await update.message.reply_text(_conn_error_text(), reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {type(e).__name__}", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    accounts = r.json() if r.status_code == 200 else []
    if not accounts:
        await update.message.reply_text("❌ No registered accounts.", reply_markup=MAIN_KEYBOARD)
        context.user_data.clear(); return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Leave all {len(accounts)}", callback_data="leave_confirm"),
        InlineKeyboardButton("❌ Cancel",                    callback_data="leave_cancel"),
    ]])
    await update.message.reply_text(
        f"🔴 Target: {ref}\n\n"
        f"This will remove **ALL {len(accounts)} account(s)** from the call.\n\n"
        "Confirm?",
        reply_markup=kb,
    )
    return LEAVEVC_CONFIRM


async def leavevc_confirm(update, context):
    return await _voice_confirm(update, context, "leave")


async def leavevc_retry(update, context):
    return await _voice_retry(update, context, "leave")

# ============================================================
# FALLBACK + ERROR
# ============================================================

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤔 I didn't understand that. Use the buttons below or /help.",
        reply_markup=MAIN_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled: %s", type(context.error).__name__, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"⚠️ Error: {type(context.error).__name__}\nSend /start to reset."
            )
    except Exception:
        pass

# ============================================================
# STARTUP PROBE
# ============================================================

async def _probe_backend() -> bool:
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{BACKEND_URL}/health", timeout=5.0)
            if r.status_code == 200:
                log.info("✅ Backend reachable at %s", BACKEND_URL)
                return True
            log.warning("⚠️  Backend HTTP %s", r.status_code)
    except httpx.ConnectError:
        log.error("❌ Backend unreachable at %s", BACKEND_URL)
    except Exception as e:
        log.error("❌ Backend probe failed: %s", type(e).__name__)
    return False

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("Starting bot — backend=%s (unlimited accounts, VC=all)", BACKEND_URL)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔐 Login$"), login_entry)],
        states={
            LOGIN_LABEL: [MessageHandler(NOT_BUTTON_TEXT, login_label)],
            LOGIN_PHONE: [MessageHandler(NOT_BUTTON_TEXT, login_phone)],
            LOGIN_OTP:   [MessageHandler(NOT_BUTTON_TEXT, login_otp)],
            LOGIN_2FA:   [MessageHandler(NOT_BUTTON_TEXT, login_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    joinvc_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎙️ Join VC$"), joinvc_entry)],
        states={
            JOINVC_LINK: [MessageHandler(NOT_BUTTON_TEXT, joinvc_link)],
            JOINVC_CONFIRM: [
                CallbackQueryHandler(joinvc_confirm, pattern=r"^join_(confirm|cancel)$"),
                CallbackQueryHandler(joinvc_retry,   pattern=r"^join_retry$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )

    leavevc_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔴 Leave VC$"), leavevc_entry)],
        states={
            LEAVEVC_LINK: [MessageHandler(NOT_BUTTON_TEXT, leavevc_link)],
            LEAVEVC_CONFIRM: [
                CallbackQueryHandler(leavevc_confirm, pattern=r"^leave_(confirm|cancel)$"),
                CallbackQueryHandler(leavevc_retry,   pattern=r"^leave_retry$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )

    # Commands that never conflict with conversations
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Conversations first — their /cancel fallback must win while active
    app.add_handler(login_conv)
    app.add_handler(joinvc_conv)
    app.add_handler(leavevc_conv)

    # Top-level /cancel — only reached when nothing is active
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Button handlers
    app.add_handler(MessageHandler(filters.Regex("^📊 Status$"),   cmd_status))
    app.add_handler(MessageHandler(filters.Regex("^👥 Accounts$"), cmd_accounts))
    app.add_handler(MessageHandler(filters.Regex("^🚪 Logout$"),   cmd_logout))
    app.add_handler(MessageHandler(filters.Regex("^☁️ Backup$"),   cmd_backup))
    app.add_handler(MessageHandler(filters.Regex("^♻️ Restore$"),  cmd_restore))
    app.add_handler(MessageHandler(filters.Regex("^❔ Help$"),      cmd_help))

    # Callbacks
    app.add_handler(CallbackQueryHandler(restore_callback, pattern=r"^restore_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(logout_callback,  pattern=r"^logout_(.+)$"))

    # Fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))
    app.add_error_handler(error_handler)

    asyncio.run(_probe_backend())
    log.info("Bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
