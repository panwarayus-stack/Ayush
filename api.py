"""
api.py — Telegram backend. Local db.json storage with bearer-token authentication.
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
import re
import secrets
import tempfile
import time
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
from typing import Any, AsyncIterator, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from telethon import TelegramClient, functions, types, utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    GroupcallForbiddenError,
    InviteHashEmptyError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

# Voice-chat engine — real WebRTC, not signaling-only.
from ntgcalls import ConnectionKind, ConnectionState
from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, GroupCallConfig
from pytgcalls.exceptions import (
    NoActiveGroupCall,
    NotInCallError,
)

# ============================================================
# CONFIG
# ============================================================

def _load_settings(path: pathlib.Path) -> dict[str, str]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SystemExit(f"Cannot read {path.name}; expected a valid JSON file") from None
    if not isinstance(values, dict) or any(not isinstance(value, str) for value in values.values()):
        raise SystemExit(f"{path.name} must be a JSON object with string values")
    return values


_local = _load_settings(pathlib.Path(__file__).with_name("credentials.json"))


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, _local.get(name, default))


TELEGRAM_API_ID = setting("TELEGRAM_API_ID", "31098775")
TELEGRAM_API_HASH = setting("TELEGRAM_API_HASH", "d296e8a182153939e2ce69898072c6fc")
ENCRYPTION_KEY_HEX = setting("ENCRYPTION_KEY_HEX", "4fc4c676f3e46b45eea63f9c70b7891d438c7a98222ea79c796ed0706b1fe91b")
BACKEND_API_TOKEN = setting("BACKEND_API_TOKEN", "")

MAX_ACCOUNTS: Optional[int] = None

DB_PATH = pathlib.Path(setting("DB_PATH", str(pathlib.Path(__file__).resolve().parent / "db.json"))).resolve()
LOGIN_TTL = 600
TELEGRAM_TIMEOUT = 20

BIND_HOST = "127.0.0.1"
BIND_PORT = int(setting("PORT", "11584"))

# ============================================================
# STATE
# ============================================================

app_state: dict[str, Any] = {
    "accounts": {},
    "clients": {},
    "tgcalls": {},               # account_id -> PyTgCalls
    "pending_logins": {},
    "voice_targets": {},  # account_id -> {chat_id: (failures, retry_at)}
    "storage_ready": False,
    "startup_error": None,
    "persistence_verified": False,
    "last_save_sha256": None,
    "keepalive_task": None,
    "restoring": False,
}

state_lock = asyncio.Lock()
storage_lock = asyncio.Lock()
voice_slots = asyncio.Semaphore(10)
client_locks: dict[str, asyncio.Lock] = {}
voice_locks: dict[str, asyncio.Lock] = {}

def serialize_state_change(handler):
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        async with state_lock:
            return await handler(*args, **kwargs)
    return wrapped


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


def _write_private_file(path: pathlib.Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".db.json-", delete=False) as file:
        tmp = pathlib.Path(file.name)
        try:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save_state_to_db() -> str:
    payload = json.dumps(_state_to_dict(), indent=2, sort_keys=True).encode("utf-8")
    _write_private_file(DB_PATH, payload)
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
    if not isinstance(data, dict):
        return False
    if data.get("version") != 1:
        log.error("db.json version unsupported: %s", data.get("version"))
        return False
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or any(not isinstance(a, dict) for a in accounts.values()):
        log.error("Invalid accounts in db.json")
        return False
    app_state["accounts"] = {aid: dict(a, account_id=aid) for aid, a in accounts.items()}
    app_state["persistence_verified"] = True
    log.info("Loaded %d account(s) from db.json", len(app_state["accounts"]))
    return True


def check_local_storage() -> dict[str, Any]:
    try:
        with tempfile.NamedTemporaryFile(dir=DB_PATH.parent):
            pass
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
        session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH,
        device_model="Telegram Control Backend", system_version="1.0", app_version="0.8.0",
        flood_sleep_threshold=0,
    )


async def _connect_client(account_id: str, session_string: Optional[str] = None) -> TelegramClient:
    async with _get_lock(account_id):
        existing = app_state["clients"].get(account_id)
        if existing and existing.is_connected():
            return existing
        client = _build_client(session_string)
        try:
            await asyncio.wait_for(client.connect(), TELEGRAM_TIMEOUT)
            if not await asyncio.wait_for(client.is_user_authorized(), TELEGRAM_TIMEOUT):
                raise PermissionError("Telegram session is not authorized")
        except (Exception, asyncio.CancelledError):
            await _safe_disconnect(client)
            raise
        app_state["clients"][account_id] = client
        log.info("Client connected account=%s", account_id)
        return client


async def _disconnect_client(account_id: str) -> None:
    async with voice_locks.setdefault(account_id, asyncio.Lock()):
        app_state["voice_targets"].pop(account_id, None)
        await _disconnect_client_unlocked(account_id)


async def _disconnect_client_unlocked(account_id: str) -> None:
    # Tear down the voice engine first so it releases the WebRTC transport.
    tg = app_state["tgcalls"].pop(account_id, None)
    if tg:
        try:
            for chat_id in await tg.calls:
                await _cleanup_voice_call(tg, chat_id)
        except Exception:
            log.warning("Voice cleanup failed account=%s", account_id)
    client = app_state["clients"].pop(account_id, None)
    if client:
        try:
            await _safe_disconnect(client)
        finally:
            log.info("Client disconnected account=%s", account_id)


async def get_client(account_id: str) -> TelegramClient:
    client = app_state["clients"].get(account_id)
    if not client or not client.is_connected():
        acct = app_state["accounts"].get(account_id, {})
        if acct.get("status") == "needs_reauth" or not acct.get("encrypted_session_b64"):
            raise HTTPException(409, "Telegram login required; use Login before joining VC")
        raise HTTPException(409, "Telegram session saved but disconnected; retry after reconnecting")
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
        except (PermissionError, ValueError, AuthKeyUnregisteredError) as exc:
            log.warning("Session unusable account=%s err=%s", aid, type(exc).__name__)
            acct["status"] = "needs_reauth"
        except Exception as exc:
            log.warning("Reconnect failed account=%s err=%s", aid, type(exc).__name__)
            acct["status"] = "disconnected"


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 5)
    except Exception as exc:
        log.warning("Telegram disconnect failed err=%s", type(exc).__name__)


async def _discard_pending(account_id: str) -> bool:
    pending = app_state["pending_logins"].pop(account_id, None)
    if pending is None:
        return False
    await _safe_disconnect(pending["client"])
    return True


async def _shutdown_clients() -> None:
    await asyncio.gather(*(_disconnect_client(aid) for aid in list(app_state["clients"])))
    await asyncio.gather(*(_discard_pending(aid) for aid in list(app_state["pending_logins"])))

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
    code: Optional[str] = Field(default=None, min_length=1, max_length=16)
    password: Optional[str] = Field(default=None, max_length=128)


class VoiceRequest(BaseModel):
    chat: str = Field(...)
    muted: bool = Field(default=True)
    video_stopped: bool = Field(default=True)


def _account_public(acct: dict[str, Any]) -> dict[str, Any]:
    client = app_state["clients"].get(acct.get("account_id"))
    return {
        "account_id": acct.get("account_id"),
        "label": acct.get("label", ""),
        "phone_hint": acct.get("phone_hint", ""),
        "status": acct.get("status", "unknown"),
        "session_available": bool(acct.get("encrypted_session_b64")),
        "connected": bool(client and client.is_connected()),
        "last_backup": acct.get("last_backup"),
        "created_at": acct.get("created_at"),
    }

# ============================================================
# KEEPALIVE
# ============================================================

async def _keepalive_loop() -> None:
    while True:
        await asyncio.sleep(30)
        async with state_lock:
            for aid, pending in list(app_state["pending_logins"].items()):
                if time.monotonic() - pending["created_at"] >= LOGIN_TTL:
                    await _discard_pending(aid)
        for aid, client in list(app_state["clients"].items()):
            if client.is_connected():
                continue
            async with voice_locks.setdefault(aid, asyncio.Lock()):
                if app_state["restoring"] or app_state["clients"].get(aid) is not client:
                    continue
                try:
                    await asyncio.wait_for(client.connect(), TELEGRAM_TIMEOUT)
                except Exception as exc:
                    log.warning("Telegram reconnect failed account=%s err=%s", aid, type(exc).__name__)

# ============================================================
# LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    invalid = []
    if not TELEGRAM_API_ID.isascii() or not TELEGRAM_API_ID.isdecimal() or int(TELEGRAM_API_ID) <= 0:
        invalid.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH.strip():
        invalid.append("TELEGRAM_API_HASH")
    if not BACKEND_API_TOKEN.strip():
        invalid.append("BACKEND_API_TOKEN")
    try:
        _get_aesgcm()
    except ValueError:
        invalid.append("ENCRYPTION_KEY_HEX")
    if invalid:
        raise RuntimeError("Missing or invalid API settings: " + ", ".join(invalid)
                           + ". Configure credentials.json beside api.py or set environment variables.")
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
    voice_task = asyncio.create_task(_monitor_voice_calls())
    try:
        yield
    finally:
        voice_task.cancel()
        await asyncio.gather(voice_task, return_exceptions=True)
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


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation errors can contain raw passwords, session strings, or codes.
    return JSONResponse({"detail": "Invalid request fields"}, status_code=422)


@app.middleware("http")
async def require_backend_token(request: Request, call_next):
    if request.url.path != "/health":
        supplied = request.headers.get("authorization", "").encode()
        expected = f"Bearer {BACKEND_API_TOKEN}".encode()
        if not BACKEND_API_TOKEN or not secrets.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

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
@serialize_state_change
async def create_account(payload: AccountCreate) -> dict[str, Any]:
    aid = payload.account_id
    if MAX_ACCOUNTS is not None and len(app_state["accounts"]) >= MAX_ACCOUNTS:
        raise HTTPException(409, f"Maximum of {MAX_ACCOUNTS} accounts already registered")
    if aid in app_state["accounts"]:
        raise HTTPException(409, f"Account '{aid}' already exists")
    if not payload.session_data and not payload.phone:
        raise HTTPException(400, "Either session_data or phone is required")

    now = datetime.now(timezone.utc).isoformat()
    phone_hint = payload.phone_hint or (f"••••{payload.phone[-4:]}" if payload.phone else "")
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
        app_state["accounts"].pop(aid, None)
        await _disconnect_client(aid)
        log.error("db.json save failed: %s", type(exc).__name__)
        raise HTTPException(503, "Account could not be saved") from exc
    log.info("Account registered id=%s status=%s", aid, acct["status"])
    return _account_public(acct)


@app.post("/accounts/{account_id}/login/send_code")
@serialize_state_change
async def send_login_code(account_id: str, payload: SendCodeRequest) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")
    if acct.get("encrypted_session_b64"):
        raise HTTPException(409, "Account already has a session; logout first")

    await _discard_pending(account_id)
    client = _build_client()
    try:
        await asyncio.wait_for(client.connect(), TELEGRAM_TIMEOUT)
        sent = await asyncio.wait_for(client.send_code_request(payload.phone), TELEGRAM_TIMEOUT)
        if not getattr(sent, "phone_code_hash", None):
            raise RuntimeError("Telegram did not issue a code challenge")
    except (Exception, asyncio.CancelledError) as exc:
        await _safe_disconnect(client)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, FloodWaitError):
            raise HTTPException(429, f"Telegram rate limit: wait {exc.seconds}s",
                                headers={"Retry-After": str(exc.seconds)}) from exc
        log.warning("send_code failed account=%s err=%s", account_id, type(exc).__name__)
        raise HTTPException(502, "Telegram could not send a login code") from exc

    app_state["pending_logins"][account_id] = {
        "client": client, "phone": payload.phone,
        "phone_code_hash": sent.phone_code_hash, "created_at": time.monotonic(),
    }
    return {"sent": True, "account_id": account_id}


@app.post("/accounts/{account_id}/login/verify")
@serialize_state_change
async def verify_login_code(account_id: str, payload: VerifyCodeRequest) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")

    pending = app_state["pending_logins"].get(account_id)
    if not pending or time.monotonic() - pending["created_at"] >= LOGIN_TTL:
        await _discard_pending(account_id)
        raise HTTPException(409, "No pending login; request a new code")

    client: TelegramClient = pending["client"]
    try:
        if not pending.get("verified") and not pending.get("needs_password"):
            if not payload.code:
                raise HTTPException(400, "OTP code required")
            try:
                await asyncio.wait_for(client.sign_in(
                    phone=pending["phone"], code=payload.code,
                    phone_code_hash=pending["phone_code_hash"],
                ), TELEGRAM_TIMEOUT)
                pending["verified"] = True
            except SessionPasswordNeededError:
                pending["needs_password"] = True
        if not pending.get("verified") and pending.get("needs_password"):
            if not payload.password:
                raise HTTPException(401, "2FA password required; retry with password")
            await asyncio.wait_for(client.sign_in(password=payload.password), TELEGRAM_TIMEOUT)
            pending["verified"] = True
    except HTTPException:
        raise
    except PasswordHashInvalidError:
        raise HTTPException(401, "2FA password incorrect")
    except PhoneCodeInvalidError:
        raise HTTPException(401, "OTP code is invalid")
    except PhoneCodeExpiredError:
        await _discard_pending(account_id)
        raise HTTPException(401, "OTP code has expired")
    except FloodWaitError as exc:
        raise HTTPException(429, f"Telegram rate limit: wait {exc.seconds}s")
    except AuthKeyUnregisteredError:
        raise HTTPException(409, "Session key invalid — logout and retry")
    except Exception as exc:
        log.error("Sign-in failed account=%s err=%s", account_id, type(exc).__name__)
        raise HTTPException(502, "Telegram could not complete sign-in")

    previous_account = acct.copy()
    try:
        await _refresh_account_session(account_id, client)
    except Exception as exc:
        log.error("Encryption failed: %s", type(exc).__name__)
        raise HTTPException(500, "Session encryption failed — check ENCRYPTION_KEY_HEX")

    acct["status"] = "active"
    try:
        save_state_to_db()
    except OSError as exc:
        acct.clear()
        acct.update(previous_account)
        log.error("db.json save after login failed: %s", type(exc).__name__)
        raise HTTPException(503, "Session could not be saved; retry verification") from exc

    app_state["clients"][account_id] = client
    app_state["pending_logins"].pop(account_id, None)

    log.info("Login verified account=%s", account_id)
    return _account_public(acct)


@app.post("/accounts/{account_id}/login/cancel")
@serialize_state_change
async def cancel_login(account_id: str) -> dict[str, bool]:
    return {"cancelled": await _discard_pending(account_id)}


@app.post("/accounts/{account_id}/logout")
@serialize_state_change
async def logout_account(account_id: str) -> dict[str, Any]:
    acct = app_state["accounts"].get(account_id)
    if not acct:
        raise HTTPException(404, f"Account '{account_id}' not found")
    # Persist removal before disconnecting, so a failed write leaves the account unchanged.
    previous = acct.copy()
    acct.update(encrypted_session_b64=None, plaintext_sha256=None,
                encrypted_sha256=None, status="needs_reauth")
    try:
        save_state_to_db()
    except OSError as exc:
        acct.update(previous)
        raise HTTPException(503, "Logout could not be persisted; retry logout") from exc
    await _disconnect_client(account_id)
    await _discard_pending(account_id)
    return {"logged_out": True, "account_id": account_id}

# ============================================================
# VOICE CHAT — real WebRTC via pytgcalls
# ============================================================

class VoiceEngine(PyTgCalls):
    """Track the native transport instead of treating a call handle as connected.

    The callback overrides are tied to the pinned PyTgCalls version.
    """

    def __init__(self, client: TelegramClient, account_id: str):
        super().__init__(client)
        self.account_id = account_id
        self.telegram_client = client
        self.connected_chats: set[int] = set()

    async def resolve_chat_id(self, chat_id):
        resolved = await super().resolve_chat_id(chat_id)
        _, peer_type = utils.resolve_id(resolved)
        if peer_type is types.PeerChannel:
            # StringSession does not persist channel access hashes across restarts.
            # Populate Telethon's cache from dialogs if the numeric peer is unknown.
            try:
                await self.telegram_client.get_input_entity(resolved)
            except ValueError:
                async def find_dialog():
                    async for dialog in self.telegram_client.iter_dialogs():
                        if dialog.id == resolved:
                            return True
                    return False

                if not await asyncio.wait_for(find_dialog(), TELEGRAM_TIMEOUT):
                    raise HTTPException(403, "Private channel not found in this account’s Telegram dialogs. Verify membership and copy a post link from inside the channel.")
        return resolved

    async def _handle_connection_changed(self, chat_id, net_state):
        if net_state.kind != ConnectionKind.NORMAL:
            return
        if net_state.state == ConnectionState.CONNECTED:
            self.connected_chats.add(chat_id)
        else:
            self.connected_chats.discard(chat_id)
        log.info("Voice transport account=%s chat=%s state=%s",
                 self.account_id, chat_id, net_state.state.name)
        waiter = self._wait_connect.get(chat_id)
        if waiter is None or not waiter.done():
            await super()._handle_connection_changed(chat_id, net_state)

    async def _handle_mtproto_updates(self, update):
        if isinstance(update, ChatUpdate) and update.status & ChatUpdate.Status.LEFT_CALL:
            self.connected_chats.discard(update.chat_id)
            app_state["voice_targets"].get(self.account_id, {}).pop(update.chat_id, None)
        await super()._handle_mtproto_updates(update)

    async def connected_to(self, chat_id: int) -> bool:
        return chat_id in self.connected_chats and chat_id in await self.calls

    async def leave_call(self, chat_id, close=False):
        try:
            await super().leave_call(chat_id, close=close)
        finally:
            self.connected_chats.discard(chat_id)


async def _get_tgcalls(account_id: str) -> VoiceEngine:
    """
    Return (and lazily start) the PyTgCalls engine for one account.

    PyTgCalls wraps ntgcalls, which performs the actual DTLS/ICE
    handshake and maintains the media transport. The backend monitors
    transport state and retries requested joins after a disconnect.
    """
    if account_id in app_state["tgcalls"]:
        await get_client(account_id)
        return app_state["tgcalls"][account_id]

    acct = app_state["accounts"].get(account_id, {})
    if acct.get("status") == "disconnected" and account_id not in app_state["clients"]:
        session = _session_string_from_account(acct)
        if session:
            try:
                await _connect_client(account_id, session)
                acct["status"] = "active"
            except (PermissionError, ValueError, AuthKeyUnregisteredError):
                acct["status"] = "needs_reauth"
            except Exception:
                log.warning("Reconnect on join failed account=%s", account_id)
    client = await get_client(account_id)
    tg = VoiceEngine(client, account_id)
    await asyncio.wait_for(tg.start(), TELEGRAM_TIMEOUT)
    app_state["tgcalls"][account_id] = tg
    log.info("PyTgCalls started account=%s", account_id)
    return tg


async def _cleanup_voice_call(tg: VoiceEngine, chat_id: int) -> None:
    try:
        await asyncio.wait_for(tg.leave_call(chat_id), timeout=5)
    except (NotInCallError, NoActiveGroupCall):
        pass
    except Exception as exc:
        log.warning("Voice cleanup failed chat=%s err=%s", chat_id, type(exc).__name__)


async def _connect_voice_call(tg: VoiceEngine, chat_id: int) -> None:
    if await tg.connected_to(chat_id):
        return
    if chat_id in await tg.calls:
        await _cleanup_voice_call(tg, chat_id)
    try:
        # The library negotiates with Telegram using native transport parameters.
        # No capture devices or outgoing media are configured, even before mute.
        await asyncio.wait_for(tg.play(
            chat_id, stream=None, config=GroupCallConfig(auto_start=False),
        ), timeout=30)
        if not await tg.connected_to(chat_id):
            raise RuntimeError("Telegram voice transport did not connect")
        await asyncio.wait_for(tg.mute(chat_id), timeout=5)
        if not await tg.connected_to(chat_id):
            raise RuntimeError("Telegram voice transport disconnected during join")
    except BaseException:
        # wait_for cancels play on timeout; the library may have already opened a transport.
        await _cleanup_voice_call(tg, chat_id)
        raise


async def _monitor_voice_calls() -> None:
    while True:
        await asyncio.sleep(10)
        await asyncio.gather(*(
            _recover_voice_calls(aid) for aid in list(app_state["voice_targets"])
        ))


async def _recover_voice_calls(account_id: str) -> None:
    async with voice_locks.setdefault(account_id, asyncio.Lock()):
        if app_state.get("restoring"):
            return
        targets = app_state["voice_targets"].get(account_id)
        tg = app_state["tgcalls"].get(account_id)
        client = app_state["clients"].get(account_id)
        if not targets or tg is None or not client or not client.is_connected():
            return
        try:
            for chat_id in list(targets):
                if chat_id not in targets:
                    continue
                connected = await tg.connected_to(chat_id)
                retry = targets.get(chat_id)
                if retry is None:
                    continue
                if connected:
                    targets[chat_id] = (0, 0.0)
                    continue
                failures, retry_at = retry
                if time.monotonic() < retry_at:
                    continue
                log.warning("Voice transport lost; attempting reconnect account=%s chat=%s", account_id, chat_id)
                try:
                    async with voice_slots:
                        if not app_state.get("restoring"):
                            await _connect_voice_call(tg, chat_id)
                except (NoActiveGroupCall, ChannelPrivateError, GroupcallForbiddenError):
                    targets.pop(chat_id, None)
                    log.info("Voice chat ended or access revoked account=%s chat=%s", account_id, chat_id)
                except FloodWaitError as exc:
                    if chat_id in targets:
                        targets[chat_id] = (failures, time.monotonic() + max(10, exc.seconds))
                except Exception as exc:
                    failures = min(failures + 1, 5)
                    if chat_id in targets:
                        targets[chat_id] = (failures, time.monotonic() + min(300, 10 * 2 ** failures))
                    log.warning("Voice reconnect failed account=%s chat=%s err=%s", account_id, chat_id, type(exc).__name__)
                else:
                    if chat_id in targets:
                        targets[chat_id] = (0, 0.0)
        except Exception as exc:
            log.warning("Voice monitor failed account=%s err=%s", account_id, type(exc).__name__)


async def _do_join(account_id: str, chat: str, muted: bool, video_stopped: bool) -> dict[str, Any]:
    async with voice_locks.setdefault(account_id, asyncio.Lock()), voice_slots:
        if app_state["restoring"]:
            return {"ok": False, "error": "Restore in progress", "status_code": 503}
        return await _join_voice_call(account_id, chat)


def _private_invite_hash(chat: str) -> str | None:
    match = re.fullmatch(
        r"(?:(?:https?://)?(?:t\.me|telegram\.me)/)?(?:\+|joinchat/)([A-Za-z0-9_-]{1,128})/?(?:\?[^#]*)?",
        chat.strip(),
    )
    return match.group(1) if match else None


async def _resolve_voice_target(tg: VoiceEngine, chat: str, *, join_channel: bool) -> int:
    invite_hash = _private_invite_hash(chat)
    if invite_hash is None:
        return await tg.resolve_chat_id(chat)
    client = tg.telegram_client
    check = functions.messages.CheckChatInviteRequest(invite_hash)
    try:
        invite = await asyncio.wait_for(client(check), TELEGRAM_TIMEOUT)
        if not isinstance(invite, types.ChatInviteAlready):
            if not join_channel:
                raise HTTPException(403, "This account is not a member of the private channel")
            try:
                updates = await asyncio.wait_for(client(functions.messages.ImportChatInviteRequest(invite_hash)), TELEGRAM_TIMEOUT)
                joined = [entity for entity in getattr(updates, "chats", ())
                          if isinstance(entity, (types.Chat, types.Channel)) and not entity.left]
                if len(joined) == 1:
                    return utils.get_peer_id(joined[0])
            except UserAlreadyParticipantError:
                pass
            invite = await asyncio.wait_for(client(check), TELEGRAM_TIMEOUT)
        if not isinstance(invite, types.ChatInviteAlready):
            raise HTTPException(409, "Telegram has not confirmed private channel membership")
        return utils.get_peer_id(invite.chat)
    except InviteRequestSentError as exc:
        raise HTTPException(409, "Join request sent. Wait for an admin to approve it, then retry Join VC.") from exc
    except (InviteHashEmptyError, InviteHashExpiredError, InviteHashInvalidError) as exc:
        raise HTTPException(400, "Private invite link is invalid or expired. Get a fresh invite link.") from exc
    except (HTTPException, FloodWaitError, ChannelPrivateError, TimeoutError):
        raise
    except Exception as exc:
        log.warning("Private invite lookup failed err=%s", type(exc).__name__)
        raise HTTPException(502, "Telegram could not resolve or join the private channel") from exc


async def _join_voice_call(account_id: str, chat: str) -> dict[str, Any]:
    """Join without transmitting media and remember the requested connection."""
    try:
        tg = await _get_tgcalls(account_id)
        chat_id = await _resolve_voice_target(tg, chat, join_channel=True)
        if chat_id >= 0:
            return {"ok": False, "error": "A group or channel is required", "status_code": 400}
        await _connect_voice_call(tg, chat_id)
        app_state["voice_targets"].setdefault(account_id, {})[chat_id] = (0, 0.0)
        return {"ok": True}
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "status_code": exc.status_code}
    except (ChannelPrivateError, GroupcallForbiddenError):
        return {"ok": False, "error": "Telegram denied access. Check that this account belongs to the channel and can join its voice chat.", "status_code": 403}
    except TimeoutError:
        return {"ok": False, "error": "Voice transport timed out", "status_code": 504}
    except NoActiveGroupCall:
        return {
            "ok": False,
            "error": "No active voice chat in this chat",
            "status_code": 404,
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
    async with voice_locks.setdefault(account_id, asyncio.Lock()), voice_slots:
        if app_state["restoring"]:
            return {"ok": False, "error": "Restore in progress", "status_code": 503}
        return await _leave_voice_call(account_id, chat)


async def _leave_voice_call(account_id: str, chat: str) -> dict[str, Any]:
    try:
        tg = app_state["tgcalls"].get(account_id)
        if not tg:
            return {"ok": False, "error": "Account is not in any call"}
        chat_id = await _resolve_voice_target(tg, chat, join_channel=False)
        was_requested = app_state["voice_targets"].get(account_id, {}).pop(chat_id, None) is not None
        try:
            await asyncio.wait_for(tg.leave_call(chat_id), timeout=10)
        except (NotInCallError, NoActiveGroupCall):
            if not was_requested:
                raise
        return {"ok": True}
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "status_code": exc.status_code}
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
        ("private invite" if _private_invite_hash(payload.chat) else payload.chat), summary["total"], summary["succeeded"],
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
        ("private invite" if _private_invite_hash(payload.chat) else payload.chat), summary["total"], summary["succeeded"],
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
@serialize_state_change
async def storage_backup() -> dict[str, Any]:
    if not app_state["accounts"]:
        raise HTTPException(400, "No accounts to back up")
    if storage_lock.locked():
        raise HTTPException(409, "Another storage operation is in progress")
    async with storage_lock:
        try:
            now = datetime.now(timezone.utc).isoformat()
            snapshot = DB_PATH.with_name(DB_PATH.name + ".backup")
            data = _state_to_dict()
            for acct in data["accounts"].values():
                acct["last_backup"] = now
            payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
            digest = sha256_bytes(payload)
            _write_private_file(snapshot, payload)
            if sha256_bytes(snapshot.read_bytes()) != digest:
                raise RuntimeError("Backup readback hash mismatch")
            for acct in app_state["accounts"].values():
                acct["last_backup"] = now
            save_state_to_db()
            return {
                "backup_completed": True,
                "uploaded_paths": [snapshot.name],
                "manifest_sha256": digest,
                "persistence_verified": app_state["persistence_verified"],
                "note": "Local snapshot verified by readback.",
            }
        except Exception as exc:
            log.error("Backup failed: %s", type(exc).__name__)
            raise HTTPException(500, "Could not write or verify the local backup")


@app.post("/storage/restore")
@serialize_state_change
async def storage_restore() -> dict[str, Any]:
    if storage_lock.locked():
        raise HTTPException(409, "Another storage operation is in progress")
    async with storage_lock:
        app_state["restoring"] = True
        try:
            if not load_state_from_db():
                return {
                    "restore_completed": False, "accounts_restored": 0,
                    "verified": False, "reason": "Database missing or invalid",
                    "storage_persistence_verified": app_state["persistence_verified"],
                }
            await _shutdown_clients()
            await _reconnect_all_clients()
            return {
                "restore_completed": True,
                "accounts_restored": len(app_state["accounts"]),
                "verified": True,
                "storage_persistence_verified": app_state["persistence_verified"],
            }
        except Exception as exc:
            log.error("Restore failed: %s", type(exc).__name__)
            raise HTTPException(500, "Restore failed") from exc
        finally:
            app_state["restoring"] = False

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=BIND_HOST,
        port=BIND_PORT,
        log_level="info",
        timeout_graceful_shutdown=10,
    )
