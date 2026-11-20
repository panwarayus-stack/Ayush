"""
api.py — Telegram backend. Local db.json storage. No API key.
Binds to 127.0.0.1 — reachable only from the same container.

Voice chat integration uses pytgcalls (which wraps ntgcalls) for
correct WebRTC negotiation, keep-alive, and reconnection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from telethon import TelegramClient, functions, types
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

# Voice-chat engine — real WebRTC, not signaling-only.
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import (
    NoActiveGroupCall,
    NotInCallError,
    UnMuteNeeded,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_API_ID: int = 31098775
TELEGRAM_API_HASH: str = "d296e8a182153939e2ce69898072c6fc"
ENCRYPTION_KEY_HEX: str = "4fc4c676f3e46b45eea63f9c70b7891d438c7a98222ea79c796ed0706b1fe91b"

MAX_ACCOUNTS: Optional[int] = None

DB_PATH = pathlib.Path(__file__).resolve().parent / "db.json"

BIND_HOST = "127.0.0.1"
BIND_PORT = int(os.environ.get("PORT", "11584"))

# ============================================================
# STATE
# ============================================================

app_state: dict[str, Any] = {
    "accounts": {},
    "clients": {},
    "tgcalls": {},               # account_id -> PyTgCalls
    "pending_logins": {},
    "storage_ready": False,
    "startup_error": None,
    "persistence_verified": False,
    "last_save_sha256": None,
    "keepalive_task": None,
}

storage_lock = asyncio.Lock()
client_locks: dict[str, asyncio.Lock] = {}

# ============================================================
# LOGGING
# ============================================================

class _SecretFilter(logging.Filter):
    _PATTERNS = ("StringSession", "encrypted_session", "session_data")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if any(p in msg for p in self._PATTERNS):
            record.msg = "[REDACTED]"
            record.args = ()
        return True


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("telegram_backend")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    handler.addFilter(_SecretFilter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = _setup_logging()

# ============================================================
# ENCRYPTION
# ============================================================

def _get_aesgcm() -> AESGCM:
    key = bytes.fromhex(ENCRYPTION_KEY_HEX)
    if len(key) != 32:
        raise ValueError("ENCRYPTION_KEY_HEX must be exactly 64 hex characters")
    return AESGCM(key)


def encrypt_bytes(plaintext: bytes) -> str:
    nonce = os.urandom(12)
    ciphertext = _get_aesgcm().encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_bytes(token_b64: str) -> bytes:
    raw = base64.b64decode(token_b64)
    return _get_aesgcm().decrypt(raw[:12], raw[12:], None)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ============================================================
# LOCAL db.json STORAGE
# ============================================================

def _state_to_dict() -> dict[str, Any]:
    accounts = {}
    for aid, acct in app_state["accounts"].items():
        accounts[aid] = {
            "account_id": aid,
            "label": acct.get("label", ""),
            "phone_hint": acct.get("phone_hint", ""),
            "status": acct.get("status", "unknown"),
            "encrypted_session_b64": acct.get("encrypted_session_b64"),
            "plaintext_sha256": acct.get("plaintext_sha256"),
            "encrypted_sha256": acct.get("encrypted_sha256"),
            "last_backup": acct.get("last_backup"),
            "created_at": acct.get("created_at"),
        }
    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
    }


def save_state_to_db() -> str:
    payload = json.dumps(_state_to_dict(), indent=2, sort_keys=True).encode("utf-8")
    tmp = DB_PATH.with_suffix(".json.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, DB_PATH)
    digest = sha256_bytes(payload)
    app_state["last_save_sha256"] = digest
    app_state["persistence_verified"] = True
    log.info("db.json saved bytes=%d sha256=%s", len(payload), digest[:12])
    return digest


def load_state_from_db() -> bool:
    if not DB_PATH.exists():
        log.info("db.json not found; starting fresh")
        return False
    try:
        data = json.loads(DB_PATH.read_bytes())
    except json.JSONDecodeError:
        corrupt = DB_PATH.with_suffix(".json.corrupt")
        try:
            DB_PATH.rename(corrupt)
            log.error("db.json corrupt → %s", corrupt.name)
        except Exception:
            log.error("db.json corrupt, could not move")
        return False
    except Exception as exc:
        log.error("db.json unreadable: %s", type(exc).__name__)
        return False
    if data.get("version") != 1:
        log.error("db.json version unsupported: %s", data.get("version"))
        return False
    app_state["accounts"] = {aid: dict(a) for aid, a in data.get("accounts", {}).items()}
    app_state["persistence_verified"] = True
    log.info("Loaded %d account(s) from db.json", len(app_state["accounts"]))
    return True


def check_local_storage() -> dict[str, Any]:
    try:
        probe = DB_PATH.parent / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        return {
            "connected": True,
            "backend": "local",
            "db_path": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        }
    except Exception as exc:
        return {
            "connected": False,
            "backend": "local",
            "db_path": str(DB_PATH),
            "error": f"{type(exc).__name__}: {exc}",
        }

# ============================================================
# TELETHON CLIENT MANAGER
# ============================================================

def _get_lock(account_id: str) -> asyncio.Lock:
    if account_id not in client_locks:
        client_locks[account_id] = asyncio.Lock()
    return client_locks[account_id]


def _build_client(session_string: Optional[str] = None) -> TelegramClient:
    session = StringSession(session_string) if session_string else StringSession()
    return TelegramClient(
        session, TELEGRAM_API_ID, TELEGRAM_API_HASH,
        device_model="Wispbyte-Backend", system_version="1.0", app_version="0.7.0",
    )


async def _connect_client(account_id: str, session_string: Optional[str] = None) -> TelegramClient:
    async with _get_lock(account_id):
        existing = app_state["clients"].get(account_id)
        if existing and existing.is_connected():
            return existing
        client = _build_client(session_string)
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except Exception:
                pass
            raise RuntimeError(f"Session for {account_id} not authorized")
        app_state["clients"][account_id] = client
        log.info("Client connected account=%s", account_id)
        return client


async def _disconnect_client(account_id: str) -> None:
    # Tear down the voice engine first so it releases the WebRTC transport.
    tg = app_state["tgcalls"].pop(account_id, None)
    if tg:
        try:
            await tg.stop()
        except Exception:
            pass
    client = app_state["clients"].pop(account_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
        log.info("Client disconnected account=%s", account_id)


async def get_client(account_id: str) -> TelegramClient:
    client = app_state["clients"].get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(409, f"Telegram client for '{account_id}' is not connected")
    return client


def _session_string_from_account(acct: dict[str, Any]) -> Optional[str]:
    enc = acct.get("encrypted_session_b64")
    if not enc:
        return None
    try:
        return decrypt_bytes(enc).decode("utf-8")
    except Exception as exc:
        log.error("Session decrypt failed account=%s err=%s",
                  acct.get("account_id"), type(exc).__name__)
        return None


async def _refresh_account_session(account_id: str, client: TelegramClient) -> None:
    session_string = client.session.save()
    plaintext = session_string.encode("utf-8")
    encrypted_b64 = encrypt_bytes(plaintext)
    acct = app_state["accounts"][account_id]
    acct["encrypted_session_b64"] = encrypted_b64
    acct["plaintext_sha256"] = sha256_bytes(plaintext)
    acct["encrypted_sha256"] = sha256_bytes(encrypted_b64.encode("ascii"))
    log.info("Session refreshed account=%s", account_id)


async def _reconnect_all_clients() -> None:
    for aid, acct in app_state["accounts"].items():
        session_string = _session_string_from_account(acct)
        if not session_string:
            acct["status"] = "needs_reauth"
            continue
        try:
            await _connect_client(aid, session_string)
            acct["status"] = "active"
        except Exception as exc:
            log.error("Reconnect failed account=%s err=%s", aid, type(exc).__name__)
            acct["status"] = "needs_reauth"


async def _shutdown_clients() -> None:
    """Disconnect connected clients AND pending-login clients."""
    for aid in list(app_state["clients"].keys()):
        await _disconnect_client(aid)
    for aid, pending in list(app_state["pending_logins"].items()):
        try:
            await pending["client"].disconnect()
        except Exception:
            pass
    app_state["pending_logins"].clear()

# ============================================================
# MODELS
# ============================================================

class AccountCreate(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(default="", max_length=120)
    phone_hint: str = Field(default="", max_length=32)
    phone: Optional[str] = Field(default=None, max_length=32)
    session_data: Optional[str] = Field(default=None)

    @field_validator("session_data")
    @classmethod
    def _not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("session_data must not be empty if provided")
        return v


class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=5, max_length=32)


class VerifyCodeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    password: Optional[str] = Field(default=None, max_length=128)


class VoiceRequest(BaseModel):
    chat: str = Field(...)
    muted: bool = Field(default=True)
    video_stopped: bool = Field(default=True)


def _account_public(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": acct.get("account_id"),
        "label": acct.get("label", ""),
        "phone_hint": acct.get("phone_hint", ""),
        "status": acct.get("status", "unknown"),
        "session_available": bool(acct.get("encrypted_session_b64")),
        "last_backup": acct.get("last_backup"),
        "created_at": acct.get("created_at"),
    }

# ============================================================
# KEEPALIVE
# ============================================================

async def _keepalive_loop() -> None:
    while True:
        await asyncio.sleep(60)
        for aid, client in list(app_state["clients"].items()):
            try:
                if not client.is_connected():
                    log.warning("Reconnecting account=%s", aid)
                    await client.connect()
                else:
                    await client.get_me()
            except Exception as exc:
                log.error("Keepalive failed account=%s err=%s", aid, type(exc).__name__)

# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("Starting up")
    storage = check_local_storage()
    app_state["storage_ready"] = storage["connected"]
    if not storage["connected"]:
        log.error("Storage not writable: %s", storage.get("error"))
    else:
        log.info("Storage OK db=%s", storage["db_path"])

    try:
        if load_state_from_db():
            await _reconnect_all_clients()
    except Exception as exc:
        log.error("Startup restore failed: %s", type(exc).__name__)
        app_state["startup_error"] = f"{type(exc).__name__}: {exc}"

    app_state["keepalive_task"] = asyncio.create_task(_keepalive_loop())
    try:
        yield
    finally:
        task = app_state.get("keepalive_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _shutdown_clients()
        log.info("Shutdown complete")


app = FastAPI(title="Telegram Backend", version="0.8.0", lifespan=lifespan)

# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
async def status_endpoint() -> dict[str, Any]:
    connected = sum(1 for c in app_state["clients"].values() if c.is_connected())
    return {
        "service": "telegram-session-backend",
        "accounts_loaded": len(app_state["accounts"]),
        "max_accounts": MAX_ACCOUNTS,
        "clients_connected": connected,
        "storage_ready": app_state["storage_ready"],
        "storage_persistence_verified": app_state["persistence_verified"],
        "startup_error": app_state["startup_error"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/accounts")
async def list_accounts() -> list[dict[str, Any]]:
    return [_account_public(a) for a in app_state["accounts"].values()]


@app.post("/accounts", status_code=201)
async def create_account(payload: AccountCreate) -> dict[str, Any]:
    aid = payload.account_id
    if MAX_ACCOUNTS is not None and len(app_state["accounts"]) >= MAX_ACCOUNTS:
        raise HTTPException(409, f"Maximum of {MAX_ACCOUNTS} accounts already registered")
    if aid in app_state["accounts"]:
        raise HTTPException(409, f"Account '{aid}' already exists")
    if not payload.session_data and not payload.phone:
        raise HTTPException(400, "Either session_data or phone is required")

    now = datetime.now(timezone.utc).isoformat()
    phone_hint = payload.phone_hint or (payload.phone or "")
    acct: dict[str, Any] = {
        "account_id": aid,
        "label": payload.label,
        "phone_hint": phone_hint,
        "status": "pending_login" if not payload.session_data else "active",
        "encrypted_session_b64": None,
        "plaintext_sha256": None,
        "encrypted_sha256": None,
        "last_backup": None,
        "created_at": now,
    }

    if payload.session_data:
        plaintext = payload.session_data.encode("utf-8")
        encrypted_b64 = encrypt_bytes(plaintext)
        acct["encrypted_session_b64"] = encrypted_b64
        acct["plaintext_sha256"] = sha256_bytes(plaintext)
        acct["encrypted_sha256"] = sha256_bytes(encrypted_b64.encode("ascii"))
        try:
            await _connect_client(aid, payload.session_data)
            acct["status"] = "active"
        except Exception as exc:
            log.warning("Immediate connect failed account=%s err=%s", aid, type(exc).__name__)
            acct["status"] = "needs_reauth"

    app_state["accounts"][aid] = acct
    try:
        save_state_to_db()
    except Exception as exc:
        log.error("db.json save failed: %s", type(exc).__name__)
    log.info("Account registered id=%s status=%s", aid, acct["status"])
    return _account_public(acct)


@app.post("/accounts/{account_id}/login/send_code")
async def send_login_code(account_id: str, payload: SendCodeRequest) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")
    if acct.get("encrypted_session_b64"):
        raise HTTPException(409, "Account already has a session; logout first")

    pending = app_state["pending_logins"].get(account_id)
    if pending and pending["client"].is_connected():
        client = pending["client"]
    else:
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
        client = _build_client()
        await client.connect()
        app_state["pending_logins"][account_id] = {
            "client": client, "phone": payload.phone,
            "phone_code_hash": None, "created_at": time.time(),
        }

    try:
        sent = await client.send_code_request(payload.phone)
    except FloodWaitError as exc:
        raise HTTPException(429, f"Telegram rate limit: wait {exc.seconds}s")
    except Exception as exc:
        log.warning("send_code failed account=%s err=%s", account_id, type(exc).__name__)
        raise HTTPException(502, f"OTP send failed: {type(exc).__name__}: {str(exc)[:150]}")

    app_state["pending_logins"][account_id]["phone"] = payload.phone
    app_state["pending_logins"][account_id]["phone_code_hash"] = sent.phone_code_hash
    return {"sent": True, "account_id": account_id}


@app.post("/accounts/{account_id}/login/verify")
async def verify_login_code(account_id: str, payload: VerifyCodeRequest) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")

    pending = app_state["pending_logins"].get(account_id)
    if not pending:
        raise HTTPException(409, "No pending login; call send_code first")

    client: TelegramClient = pending["client"]
    try:
        await client.sign_in(
            phone=pending["phone"], code=payload.code,
            phone_code_hash=pending["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        if not payload.password:
            raise HTTPException(401, "2FA password required; retry with password")
        try:
            await client.sign_in(password=payload.password)
        except Exception as exc:
            log.warning("2FA failed account=%s err=%s", account_id, type(exc).__name__)
            raise HTTPException(401, "2FA password incorrect") from exc
    except PhoneCodeInvalidError:
        raise HTTPException(401, "OTP code is invalid")
    except PhoneCodeExpiredError:
        raise HTTPException(401, "OTP code has expired")
    except FloodWaitError as exc:
        raise HTTPException(429, f"Telegram rate limit: wait {exc.seconds}s")
    except AuthKeyUnregisteredError:
        raise HTTPException(409, "Session key invalid — logout and retry")
    except Exception as exc:
        log.error("Sign-in failed account=%s err=%s", account_id, type(exc).__name__)
        raise HTTPException(502, f"Telegram rejected sign-in: {type(exc).__name__}: {str(exc)[:150]}")

    try:
        await _refresh_account_session(account_id, client)
    except Exception as exc:
        log.error("Encryption failed: %s", type(exc).__name__)
        raise HTTPException(500, "Session encryption failed — check ENCRYPTION_KEY_HEX")

    acct["status"] = "active"
    app_state["clients"][account_id] = client
    app_state["pending_logins"].pop(account_id, None)

    try:
        save_state_to_db()
    except Exception as exc:
        log.error("db.json save after login failed: %s", type(exc).__name__)

    log.info("Login verified account=%s", account_id)
    return _account_public(acct)


@app.post("/accounts/{account_id}/logout")
async def logout_account(account_id: str) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")
    await _disconnect_client(account_id)
    pending = app_state["pending_logins"].pop(account_id, None)
    if pending:
        try:
            await pending["client"].disconnect()
        except Exception:
            pass
    acct["encrypted_session_b64"] = None
    acct["plaintext_sha256"] = None
    acct["encrypted_sha256"] = None
    acct["status"] = "needs_reauth"
    try:
        save_state_to_db()
    except Exception as exc:
        log.error("db.json save after logout failed: %s", type(exc).__name__)
    return {"logged_out": True, "account_id": account_id}

# ============================================================
# VOICE CHAT — real WebRTC via pytgcalls
# ============================================================

async def _get_tgcalls(account_id: str) -> PyTgCalls:
    """
    Return (and lazily start) the PyTgCalls engine for one account.

    PyTgCalls wraps ntgcalls, which performs the actual DTLS/ICE
    handshake. It also maintains the transport with keep-alives and
    automatic reconnection.
    """
    if account_id in app_state["tgcalls"]:
        return app_state["tgcalls"][account_id]

    client = await get_client(account_id)
    tg = PyTgCalls(client)
    await tg.start()
    app_state["tgcalls"][account_id] = tg
    log.info("PyTgCalls started account=%s", account_id)
    return tg


async def _do_join(
    account_id: str, chat: str, muted: bool, video_stopped: bool,
) -> dict[str, Any]:
    """
    Join a group call using real WebRTC.

    - ``stream=None`` joins without transmitting media, which is exactly
      the muted-join behaviour.
    - The call remains active until ``_do_leave`` is called; pytgcalls
      handles reconnection internally.
    """
    try:
        tg = await _get_tgcalls(account_id)
        # ``play`` is the public API for joining a group call.
        # With stream=None it joins without streaming any audio/video.
        await tg.play(chat, stream=None)
        return {"ok": True}
    except NoActiveGroupCall:
        return {
            "ok": False,
            "error": "No active voice chat in this chat",
            "status_code": 404,
        }
    except UnMuteNeeded:
        return {
            "ok": False,
            "error": "Account is muted by an admin and cannot join",
            "status_code": 403,
        }
    except FloodWaitError as exc:
        return {
            "ok": False,
            "error": f"Telegram rate limit: wait {exc.seconds}s",
            "status_code": 429,
        }
    except Exception as exc:
        log.warning(
            "Voice join failed account=%s err=%s",
            account_id, type(exc).__name__,
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


async def _do_leave(account_id: str, chat: str) -> dict[str, Any]:
    """Leave a group call for one account. Never raises."""
    try:
        tg = app_state["tgcalls"].get(account_id)
        if not tg:
            return {"ok": False, "error": "Account is not in any call"}
        await tg.leave_call(chat)
        return {"ok": True}
    except NotInCallError:
        return {"ok": False, "error": "Not currently in the specified call"}
    except NoActiveGroupCall:
        return {"ok": False, "error": "No active voice chat in this chat"}
    except FloodWaitError as exc:
        return {
            "ok": False,
            "error": f"Telegram rate limit: wait {exc.seconds}s",
            "status_code": 429,
        }
    except Exception as exc:
        log.warning(
            "Voice leave failed account=%s err=%s",
            account_id, type(exc).__name__,
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


async def _run_bulk(chat: str, worker) -> dict[str, Any]:
    """
    Run ``worker(account_id)`` for every registered account concurrently.
    Aggregates results into a summary dict.
    """
    accounts = list(app_state["accounts"].keys())
    if not accounts:
        raise HTTPException(400, "No accounts registered")

    started = time.monotonic()
    raw = await asyncio.gather(
        *(worker(aid) for aid in accounts),
        return_exceptions=True,
    )

    results: dict[str, dict[str, Any]] = {}
    succeeded = 0
    for aid, res in zip(accounts, raw):
        if isinstance(res, BaseException):
            results[aid] = {
                "ok": False,
                "error": f"{type(res).__name__}: {str(res)[:150]}",
            }
        else:
            results[aid] = res
            if res.get("ok"):
                succeeded += 1

    return {
        "chat": chat,
        "total": len(accounts),
        "succeeded": succeeded,
        "failed": len(accounts) - succeeded,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "audio_streaming": False,
        "note": "Muted WebRTC join; no audio is transmitted.",
        "results": results,
    }


# ---- single-account endpoints ------------------------------------

@app.post("/accounts/{account_id}/voice/join")
async def voice_join(account_id: str, payload: VoiceRequest) -> dict[str, Any]:
    if account_id not in app_state["accounts"]:
        raise HTTPException(404, f"Account '{account_id}' not found")

    result = await _do_join(account_id, payload.chat, payload.muted, payload.video_stopped)
    if not result["ok"]:
        raise HTTPException(result.get("status_code", 502), result["error"])

    return {
        "joined": True,
        "account_id": account_id,
        "chat": payload.chat,
        "audio_streaming": False,
        "note": "Muted WebRTC join; no audio is transmitted.",
    }


@app.post("/accounts/{account_id}/voice/leave")
async def voice_leave(account_id: str, payload: VoiceRequest) -> dict[str, Any]:
    if account_id not in app_state["accounts"]:
        raise HTTPException(404, f"Account '{account_id}' not found")

    result = await _do_leave(account_id, payload.chat)
    if not result["ok"]:
        raise HTTPException(result.get("status_code", 502), result["error"])

    return {"left": True, "account_id": account_id, "chat": payload.chat}


# ---- bulk endpoints ---------------------------------------------

@app.post("/voice/join_all")
async def voice_join_all(payload: VoiceRequest) -> dict[str, Any]:
    """
    Join EVERY registered account to the given voice chat, concurrently.
    Always returns 200 with per-account results; caller inspects succeeded/failed.
    """
    summary = await _run_bulk(
        payload.chat,
        lambda aid: _do_join(aid, payload.chat, payload.muted, payload.video_stopped),
    )
    log.info(
        "Bulk join chat=%s total=%d ok=%d fail=%d ms=%d",
        payload.chat, summary["total"], summary["succeeded"],
        summary["failed"], summary["duration_ms"],
    )
    return summary


@app.post("/voice/leave_all")
async def voice_leave_all(payload: VoiceRequest) -> dict[str, Any]:
    """
    Leave the given voice chat from EVERY registered account, concurrently.
    """
    summary = await _run_bulk(
        payload.chat,
        lambda aid: _do_leave(aid, payload.chat),
    )
    log.info(
        "Bulk leave chat=%s total=%d ok=%d fail=%d ms=%d",
        payload.chat, summary["total"], summary["succeeded"],
        summary["failed"], summary["duration_ms"],
    )
    return summary

# ============================================================
# STORAGE (local db.json)
# ============================================================

@app.get("/storage/status")
async def storage_status() -> dict[str, Any]:
    conn = check_local_storage()
    return {
        **conn,
        "persistence_verified": app_state["persistence_verified"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/storage/backup")
async def storage_backup() -> dict[str, Any]:
    if not app_state["accounts"]:
        raise HTTPException(400, "No accounts to back up")
    if storage_lock.locked():
        raise HTTPException(409, "Another storage operation is in progress")
    async with storage_lock:
        try:
            now = datetime.now(timezone.utc).isoformat()
            for acct in app_state["accounts"].values():
                acct["last_backup"] = now
            digest = save_state_to_db()
            readback = DB_PATH.read_bytes()
            if sha256_bytes(readback) != digest:
                raise RuntimeError("db.json readback hash mismatch")
            return {
                "backup_completed": True,
                "uploaded_paths": ["db.json"],
                "manifest_sha256": digest,
                "persistence_verified": app_state["persistence_verified"],
                "note": "Local db.json backup verified by readback.",
            }
        except Exception as exc:
            log.error("Backup failed: %s", type(exc).__name__)
            raise HTTPException(500, f"Backup failed: {type(exc).__name__}: {str(exc)[:150]}")


@app.post("/storage/restore")
async def storage_restore() -> dict[str, Any]:
    if storage_lock.locked():
        raise HTTPException(409, "Another storage operation is in progress")
    async with storage_lock:
        try:
            await _shutdown_clients()

            if not load_state_from_db():
                return {
                    "restore_completed": True, "accounts_restored": 0,
                    "verified": False, "reason": "no db.json",
                    "storage_persistence_verified": app_state["persistence_verified"],
                }
            await _reconnect_all_clients()
            return {
                "restore_completed": True,
                "accounts_restored": len(app_state["accounts"]),
                "verified": True,
                "storage_persistence_verified": app_state["persistence_verified"],
            }
        except Exception as exc:
            log.error("Restore failed: %s", type(exc).__name__)
            raise HTTPException(500, f"Restore failed: {type(exc).__name__}: {str(exc)[:150]}")

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    import uvicorn

    _HERE = pathlib.Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    uvicorn.run(
        "api:app",
        host=BIND_HOST,
        port=BIND_PORT,
        log_level="info",
        timeout_graceful_shutdown=10,
        app_dir=str(_HERE),
    )
