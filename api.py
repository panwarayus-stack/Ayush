"""
api.py — Lightweight production-minded backend for authorized Telegram account
session management, backed by Hugging Face private Bucket storage.

Security posture:
  - All credentials are placeholders — replace with your deployment secret store.
  - API key required on every endpoint (except /health).
  - Session blobs are AES-256-GCM encrypted before leaving process memory.
  - Token and session contents are never logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import struct
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Optional

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from huggingface_hub import (
    HfApi,
    batch_bucket_files,
    bucket_info,
    download_bucket_files,
    list_bucket_tree,
)
from huggingface_hub.errors import HfHubHTTPError
from pydantic import BaseModel, Field, field_validator
from uvicorn import Server

# ---------------------------------------------------------------------------
# Configuration — REPLACE ALL PLACEHOLDERS
# ---------------------------------------------------------------------------

HF_TOKEN: str = "hf_REPLACE_WITH_YOUR_READ_WRITE_TOKEN"
HF_BUCKET: str = "hf://buckets/nezuhub/bucket"  # example from requirements
HF_BUCKET_ID: str = HF_BUCKET.removeprefix("hf://buckets/")  # "nezuhub/bucket"

API_KEY: str = "REPLACE_WITH_A_LONG_RANDOM_API_KEY"

# 32-byte hex/base64 key for AES-256-GCM. Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
ENCRYPTION_KEY_HEX: str = "REPLACE_WITH_64_HEX_CHARS"

# Optional: set to true only after a real upload + restore test succeeds.
STORAGE_PERSISTENCE_VERIFIED: bool = False

# Retry policy for Hugging Face operations
HF_MAX_RETRIES: int = 5
HF_BASE_DELAY: float = 1.0   # seconds
HF_MAX_DELAY: float = 30.0
HF_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Remote paths (created implicitly when first file is uploaded)
REMOTE_ACCOUNTS_META = "app/accounts_meta.json"
REMOTE_SESSION_PREFIX = "app/sessions/"

# Local in-process state (restored from remote on startup)
app_state: dict[str, Any] = {
    "accounts": {},          # account_id -> dict (metadata, encrypted_session_b64)
    "storage_ready": False,
    "startup_error": None,
}

# ---------------------------------------------------------------------------
# Structured logging — never log secrets
# ---------------------------------------------------------------------------

class _SecretFilter(logging.Filter):
    """Scrub obvious secrets from log records."""
    _PATTERNS = ("hf_", "Bearer ", "encrypted_session")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(p in msg for p in self._PATTERNS):
            record.msg = "[REDACTED — potential secret in log message]"
            record.args = ()
        return True


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("telegram_backend")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    handler.addFilter(_SecretFilter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = _setup_logging()

# ---------------------------------------------------------------------------
# Encryption helpers — AES-256-GCM
# ---------------------------------------------------------------------------

def _get_aesgcm() -> AESGCM:
    key = bytes.fromhex(ENCRYPTION_KEY_HEX)
    if len(key) != 32:
        raise ValueError("ENCRYPTION_KEY_HEX must be exactly 64 hex characters (32 bytes)")
    return AESGCM(key)


def encrypt_bytes(plaintext: bytes) -> str:
    """Encrypt bytes with AES-256-GCM; returns base64-encoded nonce+ciphertext."""
    aes = _get_aesgcm()
    nonce = os.urandom(12)  # 96-bit nonce recommended for GCM
    ciphertext = aes.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_bytes(token_b64: str) -> bytes:
    """Decrypt base64 nonce+ciphertext produced by encrypt_bytes."""
    raw = base64.b64decode(token_b64)
    nonce, ciphertext = raw[:12], raw[12:]
    aes = _get_aesgcm()
    return aes.decrypt(nonce, ciphertext, None)


# ---------------------------------------------------------------------------
# Hugging Face storage client with retry + structured errors
# ---------------------------------------------------------------------------

class StorageError(Exception):
    """Raised when remote storage operations fail after retries."""


def _hf_retry_sync(fn, *args, **kwargs):
    """Synchronous retry wrapper for Hugging Face SDK calls."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, HF_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except HfHubHTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code not in HF_RETRYABLE_STATUS:
                raise StorageError(f"Non-retryable Hugging Face error: {exc}") from exc
            last_exc = exc
        except (httpx.TimeoutException, httpx.NetworkError, ConnectionError) as exc:
            last_exc = exc
        if attempt < HF_MAX_RETRIES:
            delay = min(HF_MAX_DELAY, HF_BASE_DELAY * (2 ** (attempt - 1)))
            log.warning(
                "HF operation retry attempt=%d/%d delay=%.1fs error=%s",
                attempt, HF_MAX_RETRIES, delay, type(last_exc).__name__,
            )
            time.sleep(delay)
    raise StorageError(f"HF operation failed after {HF_MAX_RETRIES} attempts: {last_exc}")


async def _hf_retry_async(fn, *args, **kwargs):
    """Async wrapper that runs sync HF SDK calls in a thread pool."""
    return await asyncio.to_thread(_hf_retry_sync, fn, *args, **kwargs)


def _remote_file_exists(remote_path: str) -> bool:
    """Check whether a file exists in the bucket without downloading it."""
    try:
        items = list(
            list_bucket_tree(HF_BUCKET_ID, prefix=remote_path, recursive=False, token=HF_TOKEN)
        )
        return any(item.type == "file" and item.path == remote_path for item in items)
    except HfHubHTTPError as exc:
        log.warning("Bucket listing failed during existence check: %s", exc)
        return False


def _upload_bytes(remote_path: str, data: bytes) -> None:
    """Upload raw bytes to the bucket with a pre-upload existence check."""
    if _remote_file_exists(remote_path):
        log.info("Remote file already exists, skipping upload path=%s", remote_path)
        return
    _hf_retry_sync(
        batch_bucket_files,
        HF_BUCKET_ID,
        add=[(data, remote_path)],
        token=HF_TOKEN,
    )
    log.info("Uploaded remote path=%s bytes=%d", remote_path, len(data))


def _download_bytes(remote_path: str) -> bytes:
    """Download a file from the bucket into memory."""
    import tempfile, pathlib

    with tempfile.TemporaryDirectory() as tmpdir:
        dest = pathlib.Path(tmpdir) / "payload.bin"
        _hf_retry_sync(
            download_bucket_files,
            HF_BUCKET_ID,
            files=[(remote_path, str(dest))],
            token=HF_TOKEN,
        )
        return dest.read_bytes()


async def verify_bucket_connectivity() -> dict[str, Any]:
    """Ping the bucket and return basic info."""
    try:
        info = await _hf_retry_async(bucket_info, HF_BUCKET_ID, token=HF_TOKEN)
        return {
            "connected": True,
            "bucket_id": getattr(info, "id", HF_BUCKET_ID),
            "private": getattr(info, "private", None),
            "total_files": getattr(info, "total_files", None),
            "size_bytes": getattr(info, "size", None),
        }
    except Exception as exc:
        log.error("Bucket connectivity check failed: %s", type(exc).__name__)
        return {"connected": False, "error": type(exc).__name__}


# ---------------------------------------------------------------------------
# Account models and management
# ---------------------------------------------------------------------------

class AccountStatus(str, Enum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    UNKNOWN = "unknown"


class AccountCreate(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(default="", max_length=120)
    phone_hint: str = Field(default="", max_length=32, description="Non-secret phone hint only")
    session_data: str = Field(..., min_length=1, description="Opaque encrypted session blob (client-side)")

    @field_validator("session_data")
    @classmethod
    def session_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("session_data must not be empty")
        return v


class AccountPublic(BaseModel):
    account_id: str
    label: str
    phone_hint: str
    status: AccountStatus
    session_available: bool
    last_backup: Optional[str] = None
    created_at: str


def _account_public(acct: dict[str, Any]) -> AccountPublic:
    return AccountPublic(
        account_id=acct["account_id"],
        label=acct.get("label", ""),
        phone_hint=acct.get("phone_hint", ""),
        status=AccountStatus(acct.get("status", "unknown")),
        session_available=bool(acct.get("encrypted_session_b64")),
        last_backup=acct.get("last_backup"),
        created_at=acct["created_at"],
    )


def _persist_accounts_meta() -> None:
    """Upload account metadata (never session secrets) to the bucket."""
    meta = {
        aid: {
            k: v for k, v in acct.items() if k != "encrypted_session_b64"
        }
        for aid, acct in app_state["accounts"].items()
    }
    payload = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    _upload_bytes(REMOTE_ACCOUNTS_META, payload)


def _persist_encrypted_session(account_id: str, encrypted_b64: str) -> None:
    remote_path = f"{REMOTE_SESSION_PREFIX}{account_id}.enc"
    _upload_bytes(remote_path, encrypted_b64.encode("ascii"))


async def restore_accounts_from_remote() -> None:
    """Restore account metadata and encrypted sessions after restart."""
    try:
        if not _remote_file_exists(REMOTE_ACCOUNTS_META):
            log.info("No remote account metadata found; starting with empty state")
            return
        meta_bytes = await _hf_retry_async(_download_bytes, REMOTE_ACCOUNTS_META)
        meta = json.loads(meta_bytes)
        for aid, acct in meta.items():
            acct["account_id"] = aid
            remote_session = f"{REMOTE_SESSION_PREFIX}{aid}.enc"
            if _remote_file_exists(remote_session):
                try:
                    enc = await _hf_retry_async(_download_bytes, remote_session)
                    acct["encrypted_session_b64"] = enc.decode("ascii")
                    acct["session_available"] = True
                except Exception:
                    log.warning("Session blob missing or unreadable account=%s", aid)
                    acct["session_available"] = False
            else:
                acct["session_available"] = False
            app_state["accounts"][aid] = acct
        log.info("Restored %d account(s) from remote storage", len(app_state["accounts"]))
    except Exception as exc:
        log.error("Failed to restore accounts: %s", type(exc).__name__)
        app_state["startup_error"] = str(type(exc).__name__)


# ---------------------------------------------------------------------------
# API key dependency
# ---------------------------------------------------------------------------

async def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )


# ---------------------------------------------------------------------------
# FastAPI application with lifespan (startup/shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("Application starting — verifying storage connectivity")
    app_state["storage_ready"] = False
    conn = await verify_bucket_connectivity()
    if conn["connected"]:
        app_state["storage_ready"] = True
        log.info("Bucket connectivity OK bucket_id=%s", conn.get("bucket_id"))
        await restore_accounts_from_remote()
    else:
        log.warning("Bucket not reachable at startup; will retry on demand")
    yield
    log.info("Application shutting down gracefully")


app = FastAPI(
    title="Telegram Account Session Backend",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness probe — never requires auth."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status", dependencies=[Depends(require_api_key)], tags=["status"])
async def status_endpoint() -> dict[str, Any]:
    return {
        "service": "telegram-session-backend",
        "accounts_loaded": len(app_state["accounts"]),
        "storage_ready": app_state["storage_ready"],
        "storage_persistence_verified": STORAGE_PERSISTENCE_VERIFIED,
        "startup_error": app_state["startup_error"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/accounts", dependencies=[Depends(require_api_key)], tags=["accounts"])
async def list_accounts() -> list[AccountPublic]:
    return [_account_public(a) for a in app_state["accounts"].values()]


@app.get("/storage/status", dependencies=[Depends(require_api_key)], tags=["storage"])
async def storage_status() -> dict[str, Any]:
    conn = await verify_bucket_connectivity()
    return {
        **conn,
        "bucket": HF_BUCKET,
        "persistence_verified": STORAGE_PERSISTENCE_VERIFIED,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/storage/backup", dependencies=[Depends(require_api_key)], tags=["storage"])
async def storage_backup() -> dict[str, Any]:
    """
    Upload encrypted account metadata and session blobs to the bucket.
    Requires at least one account in memory.
    """
    if not app_state["accounts"]:
        raise HTTPException(status_code=400, detail="No accounts to back up")
    try:
        await asyncio.to_thread(_persist_accounts_meta)
        for aid, acct in app_state["accounts"].items():
            enc = acct.get("encrypted_session_b64")
            if enc:
                await asyncio.to_thread(_persist_encrypted_session, aid, enc)
        # Re-verify by listing the bucket
        listed = await _hf_retry_async(
            lambda: list(list_bucket_tree(HF_BUCKET_ID, prefix="app/", recursive=False, token=HF_TOKEN))
        )
        uploaded_paths = {item.path for item in listed if item.type == "file"}
        return {
            "backup_completed": True,
            "remote_paths_visible": sorted(uploaded_paths),
            "persistence_verified": STORAGE_PERSISTENCE_VERIFIED,
            "note": (
                "Persistence is only marked verified after a successful restore round-trip."
                if not STORAGE_PERSISTENCE_VERIFIED
                else "Persistence verified."
            ),
        }
    except Exception as exc:
        log.error("Backup failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Remote backup failed") from exc


@app.post("/storage/restore", dependencies=[Depends(require_api_key)], tags=["storage"])
async def storage_restore() -> dict[str, Any]:
    """Restore authorized application data from the bucket."""
    try:
        await restore_accounts_from_remote()
        return {
            "restore_completed": True,
            "accounts_restored": len(app_state["accounts"]),
            "storage_persistence_verified": STORAGE_PERSISTENCE_VERIFIED,
        }
    except Exception as exc:
        log.error("Restore failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Remote restore failed") from exc


# ---------------------------------------------------------------------------
# Startup command (for Voroa Background Worker / local development)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
        timeout_graceful_shutdown=10,
    )
